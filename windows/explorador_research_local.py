"""Read-only Streamlit explorer for an extracted Research Mirror backup.

This is a historical DATA explorer, NOT the VPS's live Streamlit dashboard.
Usage: streamlit run explorador_research_local.py -- --mirror 'D:/Backups/extracted/mirror'
"""
import argparse
import csv
from pathlib import Path
import sqlite3
import pandas as pd
import streamlit as st

st.set_page_config(page_title='Reaction Research · copia histórica local',layout='wide')
parser=argparse.ArgumentParser(add_help=False)
parser.add_argument('--mirror',default='mirror')
args,_=parser.parse_known_args()
root=Path(args.mirror).expanduser().resolve()

st.title('Reaction Research · Archivo histórico local')
st.caption('Solo lectura. No inicia Redis, PM2, Binance ni estrategias LIVE. No reemplaza todavía la interfaz original completa.')
if not root.is_dir():
    st.error(f'No existe el directorio de datos: {root}. Extraé el archivo .tar.gz y señalá la carpeta mirror.')
    st.stop()

@st.cache_data(show_spinner='Indexando archivos...')
def index_files(root_text):
    root_path=Path(root_text)
    supported={'.csv','.tsv','.json','.jsonl','.ndjson','.sqlite3','.db','.parquet','.feather','.pkl','.gz'}
    files=[]
    for f in root_path.rglob('*'):
        if f.is_file() and not f.is_symlink() and (f.suffix.lower() in supported):
            files.append(f.relative_to(root_path).as_posix())
    return sorted(files)

all_files=index_files(str(root))
st.sidebar.caption(f'{len(all_files):,} archivos de datos accesibles')
search=st.sidebar.text_input('Buscar archivo/ruta',value='reaction_lab')
matches=[p for p in all_files if search.lower() in p.lower()]
st.sidebar.caption(f'{len(matches)} resultados')
if not matches:
    st.info('No hay archivos que coincidan con ese texto.'); st.stop()
selected=st.sidebar.selectbox('Archivo',matches[:3000])
file=root/selected
if not file.resolve().is_relative_to(root):
    st.error('Ruta fuera del respaldo');st.stop()
st.subheader(file.name)
st.code(selected)
st.caption(f'{file.stat().st_size/2**20:,.2f} MiB · Solo lectura')

def chart_if_possible(df):
    nums=df.select_dtypes(include='number').columns.tolist()
    if not nums:return
    with st.expander('Gráfico exploratorio (opcional)'):
        y=st.selectbox('Columna numérica',nums)
        label_cols=[c for c in df.columns if 'time' in c.lower() or 'date' in c.lower() or 'timestamp' in c.lower()]
        x=st.selectbox('Eje X', ['(índice)']+label_cols)
        s=df[[y]].copy()
        if x!='(índice)':s.index=df[x]
        st.line_chart(s)

suffix=file.suffix.lower()
try:
    if suffix in {'.sqlite3','.db'}:
        with sqlite3.connect(file.as_uri()+'?mode=ro',uri=True,timeout=5) as con:
            tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            if not tables:st.info('Base sin tablas visibles');st.stop()
            tab=st.selectbox('Tabla SQLite',tables)
            count=con.execute('SELECT COUNT(*) FROM "'+tab.replace('"','""')+'"').fetchone()[0]
            st.metric('Filas',f'{count:,}')
            offset=st.number_input('Fila inicial',min_value=0,max_value=max(0,count-1),value=0,step=100)
            sql='SELECT * FROM "'+tab.replace('"','""')+'" LIMIT 1000 OFFSET ?'
            df=pd.read_sql_query(sql,con,params=(int(offset),))
        st.dataframe(df,use_container_width=True,height=540)
        chart_if_possible(df)
    elif suffix in {'.csv','.tsv'}:
        delim='\t' if suffix=='.tsv' else ','
        df=pd.read_csv(file,sep=delim,nrows=2000,low_memory=False)
        st.caption('Vista preliminar: primeras 2.000 filas. Para análisis completo, usar pandas/SQLite sobre tu copia local.')
        st.dataframe(df,use_container_width=True,height=540)
        chart_if_possible(df)
    elif suffix=='.parquet':
        df=pd.read_parquet(file)
        st.dataframe(df.head(2000),use_container_width=True,height=540)
        chart_if_possible(df.head(2000))
    elif suffix in {'.json','.jsonl','.ndjson'}:
        with file.open('r',encoding='utf-8',errors='replace') as fp:
            st.code(fp.read(120000),language='json')
        st.caption('Vista parcial hasta 120.000 caracteres')
    else:
        st.info('Archivo binario/comprimido preservado. Podés analizarlo con Python desde la ruta indicada; no se deserializan pickles por seguridad.')
except Exception as e:
    st.error(f'No se pudo abrir en modo lectura: {type(e).__name__}: {e}')
