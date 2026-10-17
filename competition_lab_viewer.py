#!/usr/bin/env python3
"""Standalone Streamlit entry point; does not change dashboard/app.py."""
import argparse
import streamlit as st
from dashboard.views.reaction_lab_competition_view import render_competition_lab

p = argparse.ArgumentParser()
p.add_argument("--data-dir", required=True)
args = p.parse_args()
st.set_page_config(page_title="Candidate Competition Lab", layout="wide")
render_competition_lab(args.data_dir)
