#!/usr/bin/env python3
"""Parche opt-in quirúrgico, con respaldo, para Discovery con Inspector Universal.

--plan no escribe nada; --apply modifica solo vista HLL, nunca motores LIVE.
Refusa explícitamente versiones sin ancla exacta en lugar de sobreescribir.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import os
import tempfile
from pathlib import Path

TARGET='dashboard/views/hll_structural_discovery_view.py'
ANCHOR='        if universal_result is not None:\n            resistance_ui.render_audit(universal_result)\n'
INSERT=("        # Unified V1.1: panel opt-in, solo lectura, debajo del gráfico existente.\n"
        "        if selected.name=='full290_v1':\n"
        "            from dashboard.views import hll_unified_lifecycle_family_view_v11 as family_lifecycle_ui\n"
        "            lifecycle_selected = r if in_episodes else (member if overlap is not None else row)\n"
        "            family_lifecycle_ui.render(root, lifecycle_selected, reveal_post=show_post)\n")
MARKER='hll_unified_lifecycle_family_view_v11'


def proposal(text):
    if MARKER in text:
        raise ValueError('El panel Unified V1.1 ya figura en Discovery; no duplicar')
    if text.count(ANCHOR)!=1:
        raise ValueError('La versión actual no contiene el ancla exacta del Inspector Universal; no parchear')
    if 'st.plotly_chart(fig' not in text or 'mechanism_rows is not None' not in text:
        raise ValueError('No se reconoce el Discovery PRE/POST + overlay; no parchear')
    new=text.replace(ANCHOR,ANCHOR+INSERT,1)
    ast.parse(new)
    return new


def run(target, apply=False):
    p=Path(target)
    text=p.read_text(encoding='utf-8')
    new=proposal(text)
    old_hash=hashlib.sha256(text.encode()).hexdigest()
    ret={'target':str(p),'status':'READY','current_sha256':old_hash,
         'backup':str(p.with_name(p.name+'.bak-unified-v11-'+old_hash[:12])),
         'only_one_insertion':True,'new_sha256':hashlib.sha256(new.encode()).hexdigest()}
    if apply:
        backup=Path(ret['backup'])
        if backup.exists():raise FileExistsError(f'Refusing backup overwrite: {backup}')
        # Never replace a view if the source changed during verification.
        backup.write_bytes(text.encode())
        tmp=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',dir=p.parent,prefix='.hll-v11-',suffix='.py',
                 encoding='utf-8',delete=False) as f:
                f.write(new);tmp=Path(f.name)
            if hashlib.sha256(p.read_bytes()).hexdigest()!=old_hash:
                raise ValueError('La vista cambió durante el parche; abortar')
            os.replace(tmp,p)
        finally:
            if tmp is not None and tmp.exists():tmp.unlink()
        ret['status']='APPLIED'
    print('HLL_UNIFIED_V11_DASHBOARD '+json.dumps(ret,ensure_ascii=False))
    return ret


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--apply',action='store_true')
    p.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    a=p.parse_args(argv)
    run(a.root/TARGET,apply=a.apply)

if __name__=='__main__':main()
