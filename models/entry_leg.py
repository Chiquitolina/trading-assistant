from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Optional

from models.execution_variant import ExecutionVariant


class EntryLegIdentityMutationError(AttributeError):
    pass


@dataclass(frozen=True)
class EntryLegIdentity:
    leg_id: str
    aggregate_position_id: str
    symbol: str
    setup_id: str
    variant: ExecutionVariant
    signal_reason: str
    arm_reason: Optional[str]
    execution_reason: str
    signal_ts: int
    entry_ts: int

    def __post_init__(self):
        for name in (
            "leg_id", "aggregate_position_id", "symbol", "setup_id",
            "signal_reason", "execution_reason",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if self.arm_reason is not None:
            if not isinstance(self.arm_reason, str) or not self.arm_reason.strip():
                raise ValueError("arm_reason must be None or a non-empty string")
            object.__setattr__(self, "arm_reason", self.arm_reason.strip())
        for name in ("signal_ts", "entry_ts"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        try:
            variant = ExecutionVariant.parse(self.variant)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid entry leg variant: {self.variant}") from exc
        if variant not in {
            ExecutionVariant.BUCKET_V1,
            ExecutionVariant.BUCKET_V2,
            ExecutionVariant.LEGACY_UNKNOWN,
        }:
            raise ValueError(f"invalid entry leg variant: {variant}")
        object.__setattr__(self, "variant", variant)


@dataclass
class EntryLeg:
    identity: EntryLegIdentity
    requested_entry: float
    real_entry: float
    initial_quantity: float
    remaining_quantity: float
    size_fraction: float
    tp: float
    sl: float
    closed_quantity: float = 0.0
    management_profile: Optional[str] = None
    signal_context: dict[str, Any] = field(default_factory=dict)
    status: str = "OPEN"
    tp_order_id: Optional[int] = None
    sl_order_id: Optional[int] = None
    ever_combined: bool = False
    overlapped_with_other_leg: bool = False
    exit_fills: list[dict[str, Any]] = field(default_factory=list)
    entry_fees: float = 0.0
    exit_fees: float = 0.0
    exit_reason: Optional[str] = None
    exit_ts: Optional[int] = None
    _identity_locked: bool = field(default=False, init=False, repr=False, compare=False)

    def __setattr__(self, name, value):
        if name == "identity" and getattr(self, "_identity_locked", False):
            raise EntryLegIdentityMutationError("EntryLeg identity cannot be replaced")
        super().__setattr__(name, value)

    def __post_init__(self):
        if not isinstance(self.identity, EntryLegIdentity):
            raise TypeError("identity must be an EntryLegIdentity")
        self.signal_context = deepcopy(self.signal_context or {})
        self.exit_fills = deepcopy(self.exit_fills or [])
        self.validate_quantities()
        if not 0 < float(self.size_fraction) <= 1:
            raise ValueError("size_fraction must be in (0, 1]")
        if float(self.tp) <= 0 or float(self.sl) <= 0:
            raise ValueError("tp and sl must be greater than zero")
        object.__setattr__(self, "_identity_locked", True)

    def validate_quantities(self):
        initial = float(self.initial_quantity)
        remaining = float(self.remaining_quantity)
        closed = float(self.closed_quantity)
        if initial <= 0:
            raise ValueError("initial_quantity must be greater than zero")
        if remaining < 0 or closed < 0:
            raise ValueError("remaining_quantity and closed_quantity cannot be negative")
        tolerance = max(1e-9, abs(initial) * 1e-9)
        if remaining + closed > initial + tolerance:
            raise ValueError("remaining_quantity + closed_quantity exceeds initial_quantity")

    @property
    def leg_id(self): return self.identity.leg_id
    @property
    def aggregate_position_id(self): return self.identity.aggregate_position_id
    @property
    def symbol(self): return self.identity.symbol
    @property
    def setup_id(self): return self.identity.setup_id
    @property
    def variant(self): return self.identity.variant
    @property
    def signal_reason(self): return self.identity.signal_reason
    @property
    def arm_reason(self): return self.identity.arm_reason
    @property
    def execution_reason(self): return self.identity.execution_reason
    @property
    def signal_ts(self): return self.identity.signal_ts
    @property
    def entry_ts(self): return self.identity.entry_ts

    @property
    def deduplication_key(self):
        return self.symbol, self.variant, self.setup_id

    def to_dict(self):
        return {
            "leg_id": self.leg_id, "aggregate_position_id": self.aggregate_position_id,
            "symbol": self.symbol, "setup_id": self.setup_id,
            "variant": self.variant.value, "signal_reason": self.signal_reason,
            "arm_reason": self.arm_reason, "execution_reason": self.execution_reason,
            "signal_ts": self.signal_ts, "entry_ts": self.entry_ts,
            "requested_entry": self.requested_entry, "real_entry": self.real_entry,
            "initial_quantity": self.initial_quantity,
            "remaining_quantity": self.remaining_quantity,
            "closed_quantity": self.closed_quantity, "size_fraction": self.size_fraction,
            "tp": self.tp, "sl": self.sl, "management_profile": self.management_profile,
            "signal_context": deepcopy(self.signal_context), "status": self.status,
            "tp_order_id": self.tp_order_id, "sl_order_id": self.sl_order_id,
            "ever_combined": self.ever_combined,
            "overlapped_with_other_leg": self.overlapped_with_other_leg,
            "exit_fills": deepcopy(self.exit_fills), "entry_fees": self.entry_fees,
            "exit_fees": self.exit_fees, "exit_reason": self.exit_reason,
            "exit_ts": self.exit_ts,
        }

    @classmethod
    def from_dict(cls, data):
        values = deepcopy(data)
        identity = EntryLegIdentity(
            **{name: values.pop(name) for name in (
                "leg_id", "aggregate_position_id", "symbol", "setup_id", "variant",
                "signal_reason", "arm_reason", "execution_reason", "signal_ts", "entry_ts",
            )}
        )
        return cls(identity=identity, **values)
