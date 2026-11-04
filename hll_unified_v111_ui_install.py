"""Instalador guardado HLL Unified UI 1.1.1: solo cambia el módulo de panel.

Nunca modifica Discovery ni detectores; `--plan` no escribe.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

SOURCE_REL='updates/unified_v111/dashboard/views/hll_unified_lifecycle_family_view_v11.py'
TARGET_REL='dashboard/views/hll_unified_lifecycle_family_view_v11.py'
OLD_SHA='fb3532300b6f4f87d3ee042372f419901c2a983e742c4ae5fb78d3d10066844b'
NEW_SHA='1d78d9f5778c8b1a10f09ec41957ea7e920972b78e331391ae1dc3b9c14d2c29'


def sha_file(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):
            h.update(b)
    return h.hexdigest()


def run(root:Path, *, apply:bool=False, expected_old:str=OLD_SHA, expected_new:str=NEW_SHA):
    root=Path(root).resolve()
    source=root/SOURCE_REL
    target=root/TARGET_REL
    if not source.is_file() or not target.is_file():
        raise FileNotFoundError(f'Falta candidato o panel existente: {source} / {target}')
    if sha_file(source)!=expected_new:
        raise ValueError('El candidato no coincide con el SHA-256 esperado; abortado')
    actual=sha_file(target)
    if actual==expected_new:
        raise ValueError('El panel V1.1.1 ya está instalado. No aplicar dos veces')
    if actual!=expected_old:
        raise ValueError(f'Panel instalado DIFERENTE al V1.1 original ({actual}). No sobrescribir; conciliar antes')
    backup=target.with_name(target.name+'.bak-v111-'+expected_old[:12])
    if backup.exists():
        raise FileExistsError(f'El respaldo ya existe: {backup}. Revisar antes de aplicar')
    status={'status':'READY' if not apply else 'APPLIED','target':str(target),
            'candidate':str(source),'original_sha256':actual,'new_sha256':expected_new,
            'backup':str(backup),'discovery_chart_unchanged':True}
    if not apply:return status
    shutil.copy2(target,backup)
    temporary=target.with_name(target.name+'.pending-v111')
    try:
        temporary.write_bytes(source.read_bytes())
        os.chmod(temporary,stat.S_IMODE(target.stat().st_mode))
        if sha_file(temporary)!=expected_new:
            raise ValueError('Fallo integridad durante preparación de reemplazo')
        os.replace(temporary,target)
    finally:
        if temporary.exists():temporary.unlink()
    if sha_file(target)!=expected_new:
        raise ValueError('El módulo instalado no coincide con el SHA esperado')
    return status


def main():
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan',action='store_true')
    group.add_argument('--apply',action='store_true')
    p.add_argument('--root',default=str(Path(__file__).resolve().parent))
    args=p.parse_args()
    try:
        print('HLL_UNIFIED_V111_UI '+json.dumps(run(Path(args.root),apply=args.apply),ensure_ascii=False))
    except (OSError,ValueError) as exc:
        print('HLL_UNIFIED_V111_UI '+json.dumps({'status':'BLOCKED','reason':str(exc)},ensure_ascii=False))
        raise SystemExit(2)

if __name__=='__main__':main()
