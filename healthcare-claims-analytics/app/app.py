"""Healthcare claims analytics app.

    streamlit run app/app.py

Pages: a filterable Dashboard and an Insights page of ten fixed questions,
plus an Ask the Data chat that appears only when an Anthropic API key is
configured. Credentials come from Streamlit secrets; see
.streamlit/secrets.toml.example.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

# Make sibling modules importable whether launched as app/app.py or through
# the repo-root streamlit_app.py entrypoint.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard          # noqa: E402
import insights           # noqa: E402

st.set_page_config(page_title="Claims Analytics", page_icon="🏥", layout="wide")


def anthropic_key_present() -> bool:
    """Ask the Data needs a paid API key; hide the page rather than show a
    dead end when none is configured."""
    try:
        if st.secrets.get("ANTHROPIC_API_KEY"):
            return True
    except Exception:
        pass
    return bool(os.getenv("ANTHROPIC_API_KEY"))


# url_path is explicit because every page callable is named render(); Streamlit
# would otherwise infer the same pathname for each and refuse to build the nav.
pages = [
    st.Page(dashboard.render, title="Dashboard", icon="📊",
            url_path="dashboard", default=True),
    st.Page(insights.render, title="Insights", icon="🔍", url_path="insights"),
]

if anthropic_key_present():
    import ask_data       # noqa: E402  (imported only when it can actually run)
    pages.append(st.Page(ask_data.render, title="Ask the Data", icon="💬",
                         url_path="ask"))

st.navigation(pages).run()
