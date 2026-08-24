"""Shared database access for the Streamlit app.

Connection details come from st.secrets["SUPABASE_DB_URL"], falling back to the
same variable in the environment (.env) for local development.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Resolve .env from the project, not the working directory, so the app behaves
# the same whether launched from here or via the repo-root entrypoint.
load_dotenv(PROJECT_ROOT / ".env")

# Every table the app is allowed to read. The Ask the Data page validates
# model-generated SQL against this set.
KNOWN_TABLES = {
    "dim_payer", "dim_provider", "dim_cpt", "dim_icd10",
    "dim_denial_reason", "dim_date", "fact_claims",
}


def connection_url() -> str:
    try:
        url = st.secrets["SUPABASE_DB_URL"]
    except Exception:
        url = os.getenv("SUPABASE_DB_URL")      # local dev via .env (loaded above)
    if not url:
        st.error(
            "No database connection configured. Copy "
            "`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` "
            "and set `SUPABASE_DB_URL`."
        )
        st.stop()
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    if "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    return url


@st.cache_resource
def get_engine():
    return create_engine(connection_url(), pool_pre_ping=True)


@st.cache_data(ttl=600, show_spinner="Querying Supabase…")
def run_query(sql: str, params: dict | None = None) -> pd.DataFrame:
    with get_engine().connect() as conn:
        return pd.read_sql_query(text(sql), conn, params=params or {})


@st.cache_data(ttl=600)
def date_bounds() -> tuple[pd.Timestamp, pd.Timestamp]:
    df = run_query("SELECT min(full_date) lo, max(full_date) hi FROM dim_date")
    return pd.Timestamp(df.lo[0]), pd.Timestamp(df.hi[0])
