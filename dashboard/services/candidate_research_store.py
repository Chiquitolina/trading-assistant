from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd

try:
    import duckdb  # type: ignore
except Exception:  # pragma: no cover - runtime dependency is optional
    duckdb = None

try:
    import pyarrow  # noqa: F401  # type: ignore
except Exception:  # pragma: no cover - runtime dependency is optional
    pyarrow = None


class CandidateResearchStore:
    """Parquet + DuckDB storage/query layer for Candidate Research.

    Heavy research code writes immutable-ish snapshots only during an explicit
    refresh (or a daily stale refresh). Streamlit exploration then queries those
    snapshots without touching Redis, scanners, swing reconstruction or market
    context builders.
    """

    SCHEMA_VERSION = 3
    PROFILE_FILES = {
        "v1_raw": "candidate_v1_raw.parquet",
        "v1_legacy": "candidate_v1_legacy.parquet",
        "v1_legacy_execution": "candidate_v1_legacy_execution.parquet",
        "v1_legacy_matrix_execution": "candidate_v1_legacy_matrix_execution.parquet",
        "v2": "candidate_v2.parquet",
    }

    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        self.root = self.base_dir / "reports" / "research" / "candidate"
        self.manifest_path = self.root / "manifest.json"

    @property
    def available(self) -> bool:
        return duckdb is not None and pyarrow is not None

    def dependency_status(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "duckdb": duckdb is not None,
            "pyarrow": pyarrow is not None,
        }

    def profile_path(self, profile: str) -> Path:
        profile = str(profile).lower()
        if profile not in self.PROFILE_FILES:
            raise ValueError(f"Unknown Candidate Research profile: {profile}")
        return self.root / self.PROFILE_FILES[profile]

    def read_manifest(self) -> Dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def snapshot_exists(self, profile: Optional[str] = None) -> bool:
        if profile is not None:
            return self.profile_path(profile).exists()
        return all(self.profile_path(name).exists() for name in self.PROFILE_FILES)

    def snapshot_age_hours(self) -> Optional[float]:
        manifest = self.read_manifest()
        built_at_epoch = manifest.get("built_at_epoch")
        try:
            built_at_epoch = float(built_at_epoch)
        except (TypeError, ValueError):
            return None
        return max(0.0, (time.time() - built_at_epoch) / 3600.0)

    def needs_refresh(self, max_age_hours: float = 20.0) -> bool:
        if not self.snapshot_exists():
            return True
        age = self.snapshot_age_hours()
        if age is None:
            return True
        return age >= float(max_age_hours)

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, dict):
            return {
                str(k): CandidateResearchStore._json_safe(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [CandidateResearchStore._json_safe(v) for v in value]
        return str(value)

    @classmethod
    def _prepare_for_parquet(cls, frame: pd.DataFrame) -> pd.DataFrame:
        """Normalize pandas object columns into Arrow-safe scalar columns.

        Research frames contain payload columns such as first_touch_60m_results
        that may be strings/None in early rows and dict/list objects later. A
        sampled dtype check can miss those late complex values, so every non-null
        object value is inspected before deciding how the column is encoded.
        """
        result = frame.copy().infer_objects()

        def _is_complex(value: Any) -> bool:
            return isinstance(value, (dict, list, tuple, set))

        def _encode_object(value: Any) -> Optional[str]:
            if value is None:
                return None
            try:
                if pd.isna(value):
                    return None
            except Exception:
                pass
            if _is_complex(value):
                return json.dumps(
                    cls._json_safe(value),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            if isinstance(value, pd.Timestamp):
                return value.isoformat()
            return str(value)

        for column in result.columns:
            series = result[column]
            if series.dtype != "object":
                continue

            non_null = series.dropna()
            if non_null.empty:
                continue

            values = non_null.tolist()
            has_complex = any(_is_complex(value) for value in values)

            # Any dict/list anywhere means Arrow needs one homogeneous scalar
            # representation for the entire column, not just those complex rows.
            if has_complex:
                result[column] = series.map(_encode_object)
                continue

            if all(isinstance(value, str) for value in values):
                continue

            if all(
                isinstance(value, (bool, int, float))
                and not isinstance(value, complex)
                for value in values
            ):
                result[column] = pd.to_numeric(series, errors="coerce")
                continue

            result[column] = series.map(_encode_object)

        return result

    @staticmethod
    def _atomic_json(payload: Dict[str, Any], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    @classmethod
    def _atomic_parquet(cls, frame: pd.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        prepared = cls._prepare_for_parquet(frame)
        prepared.to_parquet(
            tmp,
            engine="pyarrow",
            compression="zstd",
            index=False,
        )
        os.replace(tmp, path)

    def write_bundle(
        self,
        profiles: Dict[str, pd.DataFrame],
        *,
        source_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.available:
            raise RuntimeError(
                "Candidate Research Fast Store requires duckdb and pyarrow."
            )

        started = time.perf_counter()
        profile_meta: Dict[str, Any] = {}
        for profile, frame in profiles.items():
            profile = str(profile).lower()
            if profile not in self.PROFILE_FILES:
                continue
            frame = frame.copy() if frame is not None else pd.DataFrame()
            path = self.profile_path(profile)
            self._atomic_parquet(frame, path)
            event_col = (
                "candidate_v2_event_key"
                if "candidate_v2_event_key" in frame.columns
                else "candidate_v1_event_key"
            )
            profile_meta[profile] = {
                "path": str(path),
                "rows": int(len(frame)),
                "events": (
                    int(frame[event_col].astype(str).nunique())
                    if event_col in frame.columns and not frame.empty
                    else 0
                ),
                "columns": int(len(frame.columns)),
            }

        built_at = pd.Timestamp.now(tz="UTC")
        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            "built_at_utc": built_at.isoformat(),
            "built_at_epoch": float(time.time()),
            "build_seconds": float(time.perf_counter() - started),
            "profiles": profile_meta,
            "source": source_meta or {},
        }
        self._atomic_json(manifest, self.manifest_path)
        return manifest

    def _columns(self, profile: str) -> set[str]:
        if duckdb is None:
            return set()
        path = self.profile_path(profile)
        if not path.exists():
            return set()
        escaped = str(path).replace("'", "''")
        con = duckdb.connect(database=":memory:")
        try:
            result = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
            ).fetchall()
            return {str(row[0]) for row in result}
        finally:
            con.close()

    def distinct_values(self, profile: str, column: str) -> list[str]:
        if not self.available:
            return []
        columns = self._columns(profile)
        if column not in columns:
            return []
        path = self.profile_path(profile)
        escaped = str(path).replace("'", "''")
        escaped_column = column.replace('"', '""')
        con = duckdb.connect(database=":memory:")
        try:
            rows = con.execute(
                f'SELECT DISTINCT CAST("{escaped_column}" AS VARCHAR) '
                f"FROM read_parquet('{escaped}') "
                f'WHERE "{escaped_column}" IS NOT NULL ORDER BY 1'
            ).fetchall()
            return [str(row[0]) for row in rows if row and row[0] is not None]
        finally:
            con.close()


    def read_profile(self, profile: str, *, limit: int = 250000) -> Dict[str, Any]:
        """Read a bounded materialized profile through DuckDB.

        Used by fast research views such as the Legacy V1 execution matrix.
        This never invokes scanners, Redis, swing reconstruction, or path BUILD.
        """
        if not self.available:
            raise RuntimeError("duckdb + pyarrow are required for fast queries")

        profile = str(profile).lower()
        path = self.profile_path(profile)
        if not path.exists():
            return {
                "frame": pd.DataFrame(),
                "rows": 0,
                "elapsed_ms": 0.0,
            }

        escaped = str(path).replace("'", "''")
        limit = max(1, min(int(limit), 250000))
        con = duckdb.connect(database=":memory:")
        started = time.perf_counter()
        try:
            frame = con.execute(
                f"SELECT * FROM read_parquet('{escaped}') LIMIT {limit}"
            ).fetchdf()
        finally:
            con.close()

        return {
            "frame": frame,
            "rows": int(len(frame)),
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }

    def query(
        self,
        profile: str,
        *,
        side: str = "TOTAL",
        cohort: str = "TOTAL",
        room_min: Optional[float] = None,
        rsi_min: Optional[int] = None,
        strength_min: Optional[float] = None,
        market_alignment: str = "TOTAL",
        limit: int = 2000,
    ) -> Dict[str, Any]:
        if not self.available:
            raise RuntimeError("duckdb + pyarrow are required for fast queries")

        profile = str(profile).lower()
        path = self.profile_path(profile)
        if not path.exists():
            return {
                "frame": pd.DataFrame(),
                "matched": 0,
                "total": 0,
                "elapsed_ms": 0.0,
            }

        columns = self._columns(profile)
        escaped = str(path).replace("'", "''")
        where: list[str] = []
        params: list[Any] = []

        side_col = "side" if "side" in columns else ("signal" if "signal" in columns else None)
        if side_col and str(side).upper() != "TOTAL":
            where.append(f'UPPER(CAST("{side_col}" AS VARCHAR)) = ?')
            params.append(str(side).upper())

        if "candidate_v2_cohort" in columns and str(cohort).upper() != "TOTAL":
            where.append('UPPER(CAST("candidate_v2_cohort" AS VARCHAR)) = ?')
            params.append(str(cohort).upper())

        if room_min is not None and "nearest_opposing_room_pct" in columns:
            where.append('TRY_CAST("nearest_opposing_room_pct" AS DOUBLE) >= ?')
            params.append(float(room_min))

        if rsi_min is not None and "aligned_rsi_extreme_count" in columns:
            where.append('TRY_CAST("aligned_rsi_extreme_count" AS DOUBLE) >= ?')
            params.append(int(rsi_min))

        if strength_min is not None and "side_adjusted_strength_vs_btc_4h" in columns:
            where.append('TRY_CAST("side_adjusted_strength_vs_btc_4h" AS DOUBLE) >= ?')
            params.append(float(strength_min))

        if "Market alignment" in columns and str(market_alignment).upper() != "TOTAL":
            where.append('UPPER(CAST("Market alignment" AS VARCHAR)) = ?')
            params.append(str(market_alignment).upper())

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_col = None
        for candidate in (
            "candidate_v1_reaction_known_ts",
            "retest_timestamp",
            "legacy_v1_evaluated_at_ms",
        ):
            if candidate in columns:
                order_col = candidate
                break
        order_sql = f' ORDER BY TRY_CAST("{order_col}" AS BIGINT) DESC NULLS LAST' if order_col else ""
        limit = max(1, min(int(limit), 10000))

        con = duckdb.connect(database=":memory:")
        started = time.perf_counter()
        try:
            total = int(
                con.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{escaped}')"
                ).fetchone()[0]
            )
            matched = int(
                con.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{escaped}')"
                    + where_sql,
                    params,
                ).fetchone()[0]
            )
            frame = con.execute(
                f"SELECT * FROM read_parquet('{escaped}')"
                + where_sql
                + order_sql
                + f" LIMIT {limit}",
                params,
            ).fetchdf()
        finally:
            con.close()

        return {
            "frame": frame,
            "matched": matched,
            "total": total,
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }
