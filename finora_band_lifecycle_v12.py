"""Causal, append-only observation journal for the frozen V1.1 band selector.

Confirmation means first selection by this detector, not confirmation of a trade.
All observations are available at hourly closes. No intrabar ordering is inferred.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import argparse
import io
import json

import pandas as pd

from finora_chart_reconstruction_v1 import DAY, HOUR, Model, reconstruct, validate_hourly

VERSION = '1.2'
BASE = Path('reports/reaction_lab/finora_lifecycle_v12')
BAND_FIELDS = ['band_id', 'side', 'family', 'low', 'high', 'entry_edge', 'origin_ts', 'formed_ts',
               'first_detected_ts', 'first_context_ts', 'coverage_limited', 'rule']
EVENT_FIELDS = ['event_id', 'band_id', 'kind', 'bar_open_ts', 'available_ts', 'price',
                'high', 'low', 'close', 'context_ts']


def band_id(zone, model=Model()):
    identity = dict(model=asdict(model), side=zone['side'], origin_ts=int(zone['origin_ts']),
                    family=zone.get('family', 'BAND'),
                    low=float(zone['low']).hex(), high=float(zone['high']).hex())
    return sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]


def detector_snapshot(frame, at, model=Model()):
    """Sixteen hours cover the 4h pivot's left context beyond the 12-day window."""
    left = (int(at) - model.days * DAY - 16 * HOUR) // (4 * HOUR) * (4 * HOUR)
    closed = frame[(frame.timestamp >= left) & (frame.available_ts <= int(at))]
    return reconstruct(closed, int(at), model)


class Journal:
    """Track bands independently of subsequent ranking or price-side filters."""

    def __init__(self, source_start, model=Model()):
        self.source_start = int(source_start)
        self.model = model
        self.bands = {}
        self.events = []
        self.active = set()
        self.in_contact = {}
        self.selected = set()
        self.context = {}
        self.last_ts = None

    def emit(self, identity, kind, bar, price=None, context=None):
        at = int(bar.available_ts)
        token = f'{identity}|{kind}|{at}'
        self.events.append(dict(event_id=sha256(token.encode()).hexdigest()[:24], band_id=identity,
                                kind=kind, bar_open_ts=int(bar.timestamp), available_ts=at,
                                price=float(price) if price is not None else None,
                                high=float(bar.high), low=float(bar.low), close=float(bar.close),
                                context_ts=int(context) if context is not None else None))

    def tick(self, bar, selected_zones):
        at = int(bar.available_ts)
        if self.last_ts is not None and at != self.last_ts + HOUR:
            raise ValueError('El journal necesita cierres horarios consecutivos')
        self.last_ts = at
        # The birth candle finished before the band became observable. Contacts
        # start in the NEXT hourly candle, whose open is at or after detection.
        invalid = set()
        for identity in sorted(self.active):
            zone = self.bands[identity]
            if int(bar.timestamp) < zone['first_detected_ts']:
                continue
            overlap = bar.high >= zone['low'] and bar.low <= zone['high']
            if overlap and not self.in_contact.get(identity, False):
                price = min(zone['high'], max(zone['low'], float(bar.close)))
                self.emit(identity, 'CONTACT', bar, price)
            self.in_contact[identity] = bool(overlap)
            crossed = (bar.high >= zone['high'] if zone['family'] == 'GAP' else
                       (bar.close < zone['low'] if zone['side'] == 'BUY' else bar.close > zone['high']))
            if crossed:
                self.emit(identity, 'FILLED' if zone['family'] == 'GAP' else 'INVALIDATED', bar, bar.close)
                invalid.add(identity)
        self.active.difference_update(invalid)

        current = set()
        for zone in selected_zones:
            identity = band_id(zone, self.model)
            current.add(identity)
            if zone['known_ts'] > at:
                raise ValueError('El selector utilizó contexto futuro')
            if identity not in self.bands:
                formed = int(zone.get('formed_ts', zone['known_ts']))
                if not int(zone['origin_ts']) < formed <= at:
                    raise ValueError('Relojes de nacimiento inconsistentes')
                self.bands[identity] = dict(band_id=identity, side=zone['side'], family=zone.get('family', 'BAND'),
                    low=float(zone['low']), high=float(zone['high']), entry_edge=float(zone['entry_edge']),
                    origin_ts=int(zone['origin_ts']), formed_ts=formed, first_detected_ts=at,
                    first_context_ts=int(zone['known_ts']),
                    coverage_limited=at < self.source_start + self.model.days * DAY + 16 * HOUR,
                    rule=zone['rule'])
                self.active.add(identity)
                self.in_contact[identity] = False
                self.context[identity] = int(zone['known_ts'])
                self.emit(identity, 'DETECTED', bar, zone['entry_edge'], zone['known_ts'])
            elif identity not in self.active:
                # A closed-through band is terminal even if a later selector
                # would nominate the same physical candle again.
                current.discard(identity)
                continue
            elif int(zone['known_ts']) != self.context[identity]:
                self.context[identity] = int(zone['known_ts'])
                self.emit(identity, 'CONTEXT_UPDATED', bar, zone['entry_edge'], zone['known_ts'])
            if identity not in self.selected and self.bands[identity]['first_detected_ts'] != at:
                self.emit(identity, 'SELECTED', bar, zone['entry_edge'])
        for identity in sorted(self.selected - current):
            self.emit(identity, 'NOT_SELECTED', bar, self.bands[identity]['entry_edge'])
        self.selected = current

    def frames(self):
        bands = pd.DataFrame(list(self.bands.values()), columns=BAND_FIELDS)
        events = pd.DataFrame(self.events, columns=EVENT_FIELDS)
        return bands, events


def replay(frame, model=Model(), progress=None):
    source = validate_hourly(frame)
    journal = Journal(int(source.timestamp.iloc[0]), model)
    minimum = (2 * model.major_left_right + 1) * 4
    for count, bar in enumerate(source.itertuples(index=False), 1):
        if count < minimum:
            continue
        snapshot = detector_snapshot(source, int(bar.available_ts), model)
        gaps = [dict(gap, family='GAP', side='SELL', entry_edge=gap['low'],
                     rule='Hueco bajista sin llenar próximo a una banda seleccionada') for gap in snapshot['gaps']]
        journal.tick(bar, snapshot['zones'] + gaps)
        if progress and count % 240 == 0:
            progress(count, len(journal.bands))
    return journal.frames()


def _verified(folder, name, expected):
    path = folder / name
    if path.parent != folder or not expected:
        raise ValueError('Ruta o hash de datos inválido')
    content = path.read_bytes()
    if sha256(content).hexdigest() != expected:
        raise ValueError(f'Archivo alterado: {name}. Conservar los bytes de .gitattributes')
    return pd.read_csv(io.BytesIO(content), float_precision='round_trip')


def load_sources(project):
    folder = Path(project) / BASE
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != 'finora-lifecycle-v1.2':
        raise ValueError('Versión de historia desconocida')
    return manifest


def read_history(project, key):
    manifest = load_sources(project)
    meta = next(item for item in manifest['datasets'] if item['key'] == key)
    return validate_hourly(_verified(Path(project) / BASE, meta['file'], meta['sha256'])), meta


def read_journal(project, key):
    source, meta = read_history(project, key)
    folder = Path(project) / BASE
    if meta.get('model') != asdict(Model()) or meta.get('journal_source_sha256') != meta['sha256']:
        raise ValueError('El journal no corresponde a la fuente y al selector V1.1 congelado')
    bands = _verified(folder, meta['bands_file'], meta['bands_sha256'])
    events = _verified(folder, meta['events_file'], meta['events_sha256'])
    if list(bands.columns) != BAND_FIELDS or list(events.columns) != EVENT_FIELDS:
        raise ValueError('Columnas de journal desconocidas')
    if (bands.band_id.duplicated().any() or events.event_id.duplicated().any()
            or not events.band_id.isin(bands.band_id).all()
            or not events.available_ts.is_monotonic_increasing
            or not events.available_ts.eq(events.bar_open_ts + HOUR).all()
            or not bands.first_detected_ts.between(source.available_ts.iloc[0], source.available_ts.iloc[-1]).all()):
        raise ValueError('Identidades o relojes del journal inconsistentes')
    return source, bands, events, meta


def at_cutoff(source, bands, events, cutoff, visible_days=12):
    """Future invalidations, contacts and detections never reach this projection."""
    at = int(cutoff)
    if not 1 <= visible_days <= 60 or not source.available_ts.iloc[0] <= at <= source.available_ts.iloc[-1]:
        raise ValueError('Corte o historia visible fuera de cobertura')
    closed = source[source.available_ts <= at].copy()
    if closed.empty:
        raise ValueError('El corte aún no tiene velas cerradas')
    known = bands[bands.first_detected_ts <= at].copy()
    observed = events[events.available_ts <= at].copy()
    rows = []
    for zone in known.to_dict('records'):
        history = observed[observed.band_id.eq(zone['band_id'])]
        invalid = history[history.kind.eq('INVALIDATED')]
        filled = history[history.kind.eq('FILLED')]
        contacts = history[history.kind.eq('CONTACT')]
        selection = history[history.kind.isin(['DETECTED', 'SELECTED', 'NOT_SELECTED'])]
        zone.update(invalidated_ts=None if invalid.empty else int(invalid.available_ts.iloc[0]),
                    filled_ts=None if filled.empty else int(filled.available_ts.iloc[0]),
                    ended_ts=None if invalid.empty and filled.empty else int(pd.concat([invalid, filled]).available_ts.min()),
                    first_contact_ts=None if contacts.empty else int(contacts.available_ts.iloc[0]),
                    contacts=len(contacts), selected=not selection.empty and selection.kind.iloc[-1] != 'NOT_SELECTED',
                    status='INVALIDATED' if not invalid.empty else ('FILLED' if not filled.empty else
                           ('CONTACTED' if not contacts.empty else 'ACTIVE')))
        rows.append(zone)
    return dict(cutoff=at, start=max(int(source.timestamp.iloc[0]), at - visible_days * DAY),
                bars=closed[closed.timestamp >= at - visible_days * DAY].reset_index(drop=True),
                bands=rows, events=observed, source_start=int(source.timestamp.iloc[0]),
                model_days=12, visible_days=visible_days)


def export_one(project, key):
    source, meta = read_history(project, key)
    bands, events = replay(source, progress=lambda n, b: print(key, n, 'horas', b, 'bandas', flush=True))
    folder = Path(project) / BASE
    result = dict(key=key, model=asdict(Model()), journal_source_sha256=meta['sha256'],
                  journal_bands=len(bands), journal_events=len(events))
    for name, table in [('bands', bands), ('events', events)]:
        file = f'{key}_{name}.csv'
        content = table.to_csv(index=False, lineterminator='\n').encode()
        (folder / file).write_bytes(content)
        result[name + '_file'] = file
        result[name + '_sha256'] = sha256(content).hexdigest()
    return result


def build_exports(project):
    from concurrent.futures import ProcessPoolExecutor
    manifest = load_sources(project)
    with ProcessPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(export_one, str(project), item['key']) for item in manifest['datasets']]
        results = [job.result() for job in jobs]
    for item, result in zip(manifest['datasets'], results):
        item.update(result)
    manifest['journal_version'] = VERSION
    path = Path(project) / BASE / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print('JOURNALS_OK', [(r['key'], r['journal_bands'], r['journal_events']) for r in results], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='.')
    parser.add_argument('--build', action='store_true')
    args = parser.parse_args()
    if not args.build:
        parser.error('Elegir --build para reproducir el journal offline')
    build_exports(Path(args.project).resolve())
