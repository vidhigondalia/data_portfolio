"""Healthcare claims analytics app.

    streamlit run app/app.py

Two pages: a fixed Dashboard and an Ask the Data chat that generates SQL.
Connection details and the Anthropic API key come from Streamlit secrets;
see .streamlit/secrets.toml.example.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Make sibling modules importable whether launched as app/app.py or through
# the repo-root streamlit_app.py entrypoint.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ask_data           # noqa: E402
import dashboard          # noqa: E402

st.set_page_config(page_title="Claims Analytics", page_icon="🏥", layout="wide")

# url_path is explicit because both callables are named render(); Streamlit
# would otherwise infer the same pathname for both and refuse to build the nav.
pages = [
    st.Page(dashboard.render, title="Dashboard", icon="📊",
            url_path="dashboard", default=True),
    st.Page(ask_data.render, title="Ask the Data", icon="💬", url_path="ask"),
]
st.navigation(pages).run()
