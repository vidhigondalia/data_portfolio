"""Healthcare claims analytics dashboard.

    streamlit run app/app.py

Reads the connection string from st.secrets["SUPABASE_DB_URL"], falling back to
the same variable in the environment (.env) for local development. Nothing is
hardcoded; see .streamlit/secrets.toml.example.
"""

from __future__ import annotations

import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Claims Denial Dashboard", page_icon="🏥", layout="wide")

# Validated categorical slots (blue / orange / aqua), light and dark steps.
# Ordering is the colorblind-safety mechanism — assign in order, never cycle.
PALETTE = {
    "light": {"series": ["#2a78d6", "#eb6834", "#1baf7a"],
              "text": "#0b0b0b", "muted": "#52514e", "grid": "#e6e5e1"},
    "dark":  {"series": ["#3987e5", "#d95926", "#199e70"],
              "text": "#ffffff", "muted": "#c3c2b7", "grid": "#383835"},
}
# Fixed hue per payer type so a filter change never repaints the survivors.
PAYER_TYPE_ORDER = ["Medicare", "Medicaid", "Commercial"]


def active_theme() -> dict:
    try:
        base = st.context.theme.type
    except Exception:
        base = st.get_option("theme.base") or "light"
    return PALETTE["dark" if base == "dark" else "light"]


THEME = active_theme()
SERIES = dict(zip(PAYER_TYPE_ORDER, THEME["series"]))


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def connection_url() -> str:
    url = None
    try:
        url = st.secrets["SUPABASE_DB_URL"]
    except Exception:
        url = os.getenv("SUPABASE_DB_URL")      # local dev via .env
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


# WHERE fragment shared by every query, so all four visuals honour the filters.
FILTERS = """
    f.date_of_service BETWEEN :start AND :end
    AND p.payer_type = ANY(:payer_types)
"""


# ---------------------------------------------------------------------------
# Chart builders
# ---------------------------------------------------------------------------

def style(fig: go.Figure, height: int = 340) -> go.Figure:
    """Recessive chrome, transparent surface, text in ink not series colour."""
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=THEME["muted"], size=12),
        hoverlabel=dict(font_size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                    title_text="", font=dict(color=THEME["muted"])),
    )
    fig.update_xaxes(showgrid=False, zeroline=False, linecolor=THEME["grid"],
                     tickfont=dict(color=THEME["muted"]))
    fig.update_yaxes(gridcolor=THEME["grid"], zeroline=False, linecolor=THEME["grid"],
                     tickfont=dict(color=THEME["muted"]))
    return fig


def denial_rate_by_payer(df: pd.DataFrame) -> go.Figure:
    df = df.sort_values("denial_rate")
    fig = go.Figure()
    for ptype in PAYER_TYPE_ORDER:                 # fixed order == fixed colour
        sub = df[df.payer_type == ptype]
        if sub.empty:
            continue
        fig.add_trace(go.Bar(
            x=sub.denial_rate, y=sub.payer_name, orientation="h",
            name=ptype, marker_color=SERIES[ptype],
            # Direct labels: relief for the light-mode contrast warning.
            text=[f"{v:.1f}%" for v in sub.denial_rate],
            textposition="outside", textfont=dict(color=THEME["text"], size=11),
            customdata=sub[["claims", "denials"]],
            hovertemplate="<b>%{y}</b><br>Denial rate: %{x:.1f}%<br>"
                          "Claims: %{customdata[0]:,}<br>Denied: %{customdata[1]:,}<extra></extra>",
        ))
    fig.update_layout(barmode="stack", bargap=0.35)
    fig.update_xaxes(title_text="Denial rate (%)", showgrid=True,
                     gridcolor=THEME["grid"], range=[0, df.denial_rate.max() * 1.22])
    return style(fig, 380)


def preventable_split(preventable: int, not_preventable: int) -> go.Figure:
    total = preventable + not_preventable
    share = 100 * preventable / total if total else 0
    fig = go.Figure(go.Pie(
        labels=["Preventable", "Not preventable"],
        values=[preventable, not_preventable],
        hole=0.62,                                  # centre carries the headline
        marker=dict(colors=[THEME["series"][0], THEME["series"][1]],
                    line=dict(color=THEME["grid"], width=2)),  # 2px surface gap
        sort=False, direction="clockwise",
        textinfo="label+percent",
        textfont=dict(color=THEME["text"], size=12),
        hovertemplate="<b>%{label}</b><br>%{value:,} denials<br>%{percent}<extra></extra>",
    ))
    fig.add_annotation(text=f"<b>{share:.0f}%</b><br><span style='font-size:11px'>preventable</span>",
                       showarrow=False, font=dict(color=THEME["text"], size=26))
    fig.update_layout(showlegend=False)
    return style(fig, 380)


def denial_trend(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure(go.Scatter(
        x=df.month, y=df.denial_rate, mode="lines+markers",
        line=dict(color=THEME["series"][0], width=2),   # 2px line
        marker=dict(size=8, color=THEME["series"][0],
                    line=dict(color=THEME["grid"], width=2)),
        customdata=df[["claims", "denials"]],
        hovertemplate="<b>%{x|%b %Y}</b><br>Denial rate: %{y:.1f}%<br>"
                      "Claims: %{customdata[0]:,}<br>Denied: %{customdata[1]:,}<extra></extra>",
    ))
    fig.update_yaxes(title_text="Denial rate (%)", rangemode="tozero", ticksuffix="%")
    fig.update_xaxes(showgrid=False)
    fig.update_layout(hovermode="x unified")            # crosshair on a line chart
    return style(fig, 320)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.title("Claims Denial Dashboard")

lo, hi = date_bounds()
with st.sidebar:
    st.header("Filters")
    date_range = st.date_input("Date of service", value=(lo.date(), hi.date()),
                               min_value=lo.date(), max_value=hi.date())
    payer_types = st.multiselect("Payer type", PAYER_TYPE_ORDER, default=PAYER_TYPE_ORDER)
    # 100k claims over 900 CPT/ICD pairings averages ~111 each (range 81-154),
    # so 100 is selective without emptying the table; past ~125 almost nothing
    # qualifies. Recalibrate these bounds if NUM_CLAIMS changes.
    min_claims = st.slider("Min claims per CPT/ICD pairing", 50, 125, 100, step=25,
                           help="Low-volume pairings produce unstable denial rates. "
                                "Most pairings hold 80-150 claims at the current data volume.")

if not isinstance(date_range, (tuple, list)) or len(date_range) != 2:
    st.info("Select a start and end date.")
    st.stop()
if not payer_types:
    st.warning("Select at least one payer type.")
    st.stop()

params = {"start": date_range[0], "end": date_range[1], "payer_types": payer_types}

kpi = run_query(f"""
    SELECT count(*) AS claims,
           count(*) FILTER (WHERE f.claim_status = 'Denied') AS denials,
           coalesce(sum(f.billed_amount) FILTER (WHERE f.claim_status = 'Denied'), 0) AS denied_billed,
           count(*) FILTER (WHERE f.is_preventable) AS preventable,
           count(*) FILTER (WHERE f.claim_status = 'Denied' AND NOT f.is_preventable) AS not_preventable
    FROM fact_claims f JOIN dim_payer p ON f.payer_id = p.payer_id
    WHERE {FILTERS}
""", params).iloc[0]

if kpi.claims == 0:
    st.warning("No claims match these filters.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Claims", f"{int(kpi.claims):,}")
c2.metric("Denial rate", f"{100 * kpi.denials / kpi.claims:.1f}%")
c3.metric("Denied billed amount", f"${kpi.denied_billed:,.0f}")
c4.metric("Preventable denials",
          f"{100 * kpi.preventable / kpi.denials:.0f}%" if kpi.denials else "—")

st.divider()

left, right = st.columns([3, 2])

with left:
    st.subheader("Denial rate by payer")
    by_payer = run_query(f"""
        SELECT p.payer_name, p.payer_type, count(*) AS claims,
               count(*) FILTER (WHERE f.claim_status = 'Denied') AS denials,
               round(100.0 * count(*) FILTER (WHERE f.claim_status = 'Denied') / count(*), 1) AS denial_rate
        FROM fact_claims f JOIN dim_payer p ON f.payer_id = p.payer_id
        WHERE {FILTERS}
        GROUP BY 1, 2 ORDER BY denial_rate DESC
    """, params)
    st.plotly_chart(denial_rate_by_payer(by_payer), width="stretch")
    with st.expander("View as table"):     # relief for the contrast warning
        st.dataframe(by_payer, width="stretch", hide_index=True)

with right:
    st.subheader("Preventable vs not")
    st.plotly_chart(preventable_split(int(kpi.preventable), int(kpi.not_preventable)),
                    width="stretch")
    st.caption("Eligibility, coding, authorization and timely-filing denials are "
               "treated as preventable; medical-necessity denials are not.")

st.subheader("Denial rate over time")
trend = run_query(f"""
    SELECT date_trunc('month', f.date_of_service)::date AS month,
           count(*) AS claims,
           count(*) FILTER (WHERE f.claim_status = 'Denied') AS denials,
           round(100.0 * count(*) FILTER (WHERE f.claim_status = 'Denied') / count(*), 1) AS denial_rate
    FROM fact_claims f JOIN dim_payer p ON f.payer_id = p.payer_id
    WHERE {FILTERS}
    GROUP BY 1 ORDER BY 1
""", params)
st.plotly_chart(denial_trend(trend), width="stretch")

st.subheader("Top 10 denial-prone CPT / ICD-10 pairings")
pairs = run_query(f"""
    SELECT f.cpt_code, c.cpt_description, f.icd10_code, i.icd10_description,
           count(*) AS claims,
           count(*) FILTER (WHERE f.claim_status = 'Denied') AS denials,
           round(100.0 * count(*) FILTER (WHERE f.claim_status = 'Denied') / count(*), 1) AS denial_rate
    FROM fact_claims f
    JOIN dim_payer p ON f.payer_id = p.payer_id
    JOIN dim_cpt   c ON f.cpt_code  = c.cpt_code
    JOIN dim_icd10 i ON f.icd10_code = i.icd10_code
    WHERE {FILTERS}
    GROUP BY 1, 2, 3, 4
    HAVING count(*) >= :min_claims
    ORDER BY denial_rate DESC, claims DESC
    LIMIT 10
""", {**params, "min_claims": min_claims})

if pairs.empty:
    st.info(f"No CPT/ICD pairing has at least {min_claims} claims under these filters.")
else:
    st.dataframe(
        pairs, width="stretch", hide_index=True,
        column_config={
            "cpt_code": st.column_config.TextColumn("CPT"),
            "cpt_description": st.column_config.TextColumn("Procedure", width="medium"),
            "icd10_code": st.column_config.TextColumn("ICD-10"),
            "icd10_description": st.column_config.TextColumn("Diagnosis", width="medium"),
            "claims": st.column_config.NumberColumn("Claims", format="%d"),
            "denials": st.column_config.NumberColumn("Denials", format="%d"),
            "denial_rate": st.column_config.ProgressColumn(
                "Denial rate", format="%.1f%%", min_value=0,
                max_value=float(pairs.denial_rate.max())),
        },
    )
    lo_n, hi_n = int(pairs.claims.min()), int(pairs.claims.max())
    st.caption(
        f"Pairings with at least {min_claims} claims in the selected range "
        f"(these ten hold {lo_n}–{hi_n} claims each). Rates are still estimates — "
        "expect a couple of points of sampling noise either way."
    )
