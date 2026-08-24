"""Insights: ten fixed questions, each with the SQL that answers it.

Same questions you would put to a text-to-SQL agent, but the queries are
written and reviewed rather than generated — so results are deterministic,
instant, and free. Each tab shows the answer in plain English, the evidence,
and the exact SQL behind it.

Relative periods ("this year", "last 6 months") are anchored to the latest
date present in the data, not to today's date — the dataset ends in 2025, so
a literal CURRENT_DATE filter would return nothing.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from theme import PAYER_TYPE_ORDER, SERIES, THEME, style

QUESTIONS = [
    "What's our overall denial rate?",
    "Which payer has the highest denial rate?",
    "What percentage of denials are preventable?",
    "What are the top 5 denial reasons this year?",
    "Show denial rate by month for the last 6 months",
    "Which provider has the most denials?",
    "What's the average days between submission and adjudication?",
    "Which CPT and ICD-10 combination gets denied most often?",
    "How does Medicaid's denial rate compare to Commercial?",
    "Has a given provider's denial rate changed over time?",
]

TAB_LABELS = [
    "Overall rate", "By payer", "Preventable", "Top reasons", "Monthly trend",
    "By provider", "Turnaround", "CPT / ICD", "Medicaid vs Commercial", "Provider drift",
]

MIN_PAIR_CLAIMS = 100      # below this a pairing's denial rate is mostly noise


@st.cache_data(ttl=600)
def latest_date() -> pd.Timestamp:
    return pd.Timestamp(run_query("SELECT max(date_of_service) d FROM fact_claims").d[0])


def show(question: str, sql: str, params: dict | None = None) -> pd.DataFrame:
    """Render the question header, run the query, and expose the SQL."""
    st.subheader(question)
    df = run_query(sql, params)
    return df


def sql_expander(sql: str, note: str = "") -> None:
    with st.expander("SQL"):
        st.code(sql.strip(), language="sql")
        if note:
            st.caption(note)


def bar(df: pd.DataFrame, x: str, y: str, colors: list[str], suffix: str = "%",
        height: int = 380) -> go.Figure:
    """Horizontal bars with direct labels — relief for the light-mode contrast."""
    fig = go.Figure(go.Bar(
        x=df[x], y=df[y], orientation="h", marker_color=colors,
        text=[f"{v:,.1f}{suffix}" for v in df[x]], textposition="outside",
        textfont=dict(color=THEME["text"], size=11),
        hovertemplate="<b>%{y}</b><br>%{x:,.1f}" + suffix + "<extra></extra>",
    ))
    fig.update_layout(bargap=0.35, showlegend=False)
    fig.update_xaxes(showgrid=True, gridcolor=THEME["grid"],
                     range=[0, df[x].max() * 1.25])
    return style(fig, height)


def line(x, y, hover: str, height: int = 340) -> go.Figure:
    fig = go.Figure(go.Scatter(
        x=x, y=y, mode="lines+markers",
        line=dict(color=THEME["series"][0], width=2),
        marker=dict(size=8, color=THEME["series"][0],
                    line=dict(color=THEME["grid"], width=2)),
        hovertemplate=hover + "<extra></extra>",
    ))
    fig.update_layout(hovermode="x unified")
    fig.update_yaxes(rangemode="tozero", ticksuffix="%")
    return style(fig, height)


# ---------------------------------------------------------------------------
# The ten answers
# ---------------------------------------------------------------------------

def q1() -> None:
    sql = """
        SELECT count(*) AS claims,
               count(*) FILTER (WHERE claim_status = 'Denied') AS denials,
               round(100.0 * count(*) FILTER (WHERE claim_status = 'Denied')
                     / count(*), 2) AS denial_rate_pct
        FROM fact_claims
    """
    df = show(QUESTIONS[0], sql)
    r = df.iloc[0]
    st.markdown(
        f"**{r.denial_rate_pct}% of claims are denied** — {int(r.denials):,} of "
        f"{int(r.claims):,} claims over the full period."
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("Denial rate", f"{r.denial_rate_pct}%")
    c2.metric("Denied claims", f"{int(r.denials):,}")
    c3.metric("Total claims", f"{int(r.claims):,}")
    sql_expander(sql)


def q2() -> None:
    sql = """
        SELECT p.payer_name, p.payer_type, count(*) AS claims,
               count(*) FILTER (WHERE f.claim_status = 'Denied') AS denials,
               round(100.0 * count(*) FILTER (WHERE f.claim_status = 'Denied')
                     / count(*), 2) AS denial_rate_pct
        FROM fact_claims f
        JOIN dim_payer p ON f.payer_id = p.payer_id
        GROUP BY 1, 2
        ORDER BY denial_rate_pct DESC
    """
    df = show(QUESTIONS[1], sql)
    top = df.iloc[0]
    spread = top.denial_rate_pct - df.iloc[-1].denial_rate_pct
    st.markdown(
        f"**{top.payer_name}** has the highest rate at **{top.denial_rate_pct}%** "
        f"({int(top.denials):,} of {int(top.claims):,} claims). The spread across "
        f"all ten payers is {spread:.1f} points, so no single payer is a clear outlier "
        f"on overall volume."
    )
    plot = df.sort_values("denial_rate_pct")
    st.plotly_chart(bar(plot, "denial_rate_pct", "payer_name",
                        [SERIES[t] for t in plot.payer_type]), width="stretch")
    st.dataframe(df, width="stretch", hide_index=True)
    sql_expander(sql)


def q3() -> None:
    sql = """
        SELECT r.category,
               count(*) AS denials,
               f.is_preventable,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS share_pct
        FROM fact_claims f
        JOIN dim_denial_reason r ON f.denial_reason_code = r.reason_code
        WHERE f.claim_status = 'Denied'
        GROUP BY 1, 3
        ORDER BY denials DESC
    """
    df = show(QUESTIONS[2], sql)
    prev = df[df.is_preventable].denials.sum()
    total = df.denials.sum()
    st.markdown(
        f"**{100 * prev / total:.1f}% of denials are preventable** — {int(prev):,} of "
        f"{int(total):,}. Preventable means the denial reason falls under Eligibility, "
        f"Coding Error, Authorization or Timely Filing; Medical Necessity denials turn "
        f"on clinical judgement and are not counted."
    )
    fig = go.Figure(go.Pie(
        labels=["Preventable", "Not preventable"],
        values=[prev, total - prev], hole=0.62, sort=False,
        marker=dict(colors=[THEME["series"][0], THEME["series"][1]],
                    line=dict(color=THEME["grid"], width=2)),
        textinfo="label+percent", textfont=dict(color=THEME["text"], size=12),
        hovertemplate="<b>%{label}</b><br>%{value:,} denials<extra></extra>",
    ))
    fig.add_annotation(text=f"<b>{100 * prev / total:.0f}%</b><br>"
                            f"<span style='font-size:11px'>preventable</span>",
                       showarrow=False, font=dict(color=THEME["text"], size=26))
    fig.update_layout(showlegend=False)
    left, right = st.columns([2, 3])
    left.plotly_chart(style(fig, 340), width="stretch")
    right.dataframe(df, width="stretch", hide_index=True)
    sql_expander(sql)


def q4() -> None:
    year = latest_date().year
    sql = """
        SELECT f.denial_reason_code, r.reason_description, r.category,
               count(*) AS denials
        FROM fact_claims f
        JOIN dim_denial_reason r ON f.denial_reason_code = r.reason_code
        WHERE f.claim_status = 'Denied'
          AND extract(year FROM f.date_of_service) = :year
        GROUP BY 1, 2, 3
        ORDER BY denials DESC
        LIMIT 5
    """
    df = show(QUESTIONS[3], sql, {"year": year})
    top = df.iloc[0]
    st.markdown(
        f"In **{year}**, the most common denial reason was **{top.denial_reason_code} — "
        f"{top.reason_description}** ({int(top.denials):,} denials)."
    )
    plot = df.sort_values("denials")
    st.plotly_chart(
        bar(plot, "denials", "denial_reason_code",
            [THEME["series"][0]] * len(plot), suffix="", height=320),
        width="stretch")
    st.dataframe(df, width="stretch", hide_index=True)
    sql_expander(sql, f"'This year' is {year} — the most recent year in the data, "
                      "not the current calendar year.")


def q5() -> None:
    end = latest_date()
    start = (end - pd.DateOffset(months=5)).replace(day=1)
    sql = """
        SELECT date_trunc('month', date_of_service)::date AS month,
               count(*) AS claims,
               count(*) FILTER (WHERE claim_status = 'Denied') AS denials,
               round(100.0 * count(*) FILTER (WHERE claim_status = 'Denied')
                     / count(*), 2) AS denial_rate_pct
        FROM fact_claims
        WHERE date_of_service BETWEEN :start AND :end
        GROUP BY 1
        ORDER BY 1
    """
    df = show(QUESTIONS[4], sql, {"start": start.date(), "end": end.date()})
    first, last = df.iloc[0], df.iloc[-1]
    direction = "up" if last.denial_rate_pct > first.denial_rate_pct else "down"
    st.markdown(
        f"Over the last six months of data ({first.month:%b %Y} – {last.month:%b %Y}), "
        f"the denial rate moved **{direction}** from {first.denial_rate_pct}% to "
        f"**{last.denial_rate_pct}%**."
    )
    st.plotly_chart(
        line(df.month, df.denial_rate_pct, "<b>%{x|%b %Y}</b><br>Denial rate: %{y:.2f}%"),
        width="stretch")
    st.dataframe(df, width="stretch", hide_index=True)
    sql_expander(sql, f"'Last 6 months' is anchored to {end:%b %Y}, the latest date "
                      "in the data.")


def q6() -> None:
    sql = """
        SELECT pr.provider_id, pr.provider_name, pr.specialty,
               count(*) AS claims,
               count(*) FILTER (WHERE f.claim_status = 'Denied') AS denials,
               round(100.0 * count(*) FILTER (WHERE f.claim_status = 'Denied')
                     / count(*), 2) AS denial_rate_pct
        FROM fact_claims f
        JOIN dim_provider pr ON f.provider_id = pr.provider_id
        GROUP BY 1, 2, 3
        ORDER BY denials DESC
    """
    df = show(QUESTIONS[5], sql)
    top_count = df.iloc[0]
    top_rate = df.sort_values("denial_rate_pct", ascending=False).iloc[0]
    if top_rate.provider_id == top_count.provider_id:
        tail = (f" They also lead on *rate* at {top_rate.denial_rate_pct}%, so this is "
                f"not just a volume effect.")
    else:
        tail = (f" By *rate* rather than count the leader is "
                f"**{top_rate.provider_name}** at {top_rate.denial_rate_pct}% — worth "
                f"separating, since providers with more claims accumulate more denials "
                f"without necessarily performing worse.")
    st.markdown(
        f"**{top_count.provider_name}** ({top_count.provider_id}, "
        f"{top_count.specialty}) has the most denials at "
        f"**{int(top_count.denials):,}**.{tail}"
    )
    plot = df.nlargest(10, "denials").sort_values("denials")
    st.plotly_chart(
        bar(plot, "denials", "provider_name", [THEME["series"][0]] * len(plot),
            suffix="", height=380),
        width="stretch")
    st.dataframe(df, width="stretch", hide_index=True)
    sql_expander(sql)


def q7() -> None:
    sql = """
        SELECT round(avg(date_adjudicated - date_submitted), 1) AS avg_days,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY date_adjudicated - date_submitted) AS median_days,
               min(date_adjudicated - date_submitted) AS min_days,
               max(date_adjudicated - date_submitted) AS max_days,
               count(*) AS claims
        FROM fact_claims
        WHERE date_adjudicated IS NOT NULL AND date_submitted IS NOT NULL
    """
    df = show(QUESTIONS[6], sql)
    r = df.iloc[0]
    st.markdown(
        f"Claims take **{r.avg_days} days on average** from submission to "
        f"adjudication (median {r.median_days:.0f}, range {int(r.min_days)}–"
        f"{int(r.max_days)} days across {int(r.claims):,} claims)."
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("Average", f"{r.avg_days} days")
    c2.metric("Median", f"{r.median_days:.0f} days")
    c3.metric("Range", f"{int(r.min_days)}–{int(r.max_days)} days")

    by_status = run_query("""
        SELECT claim_status, round(avg(date_adjudicated - date_submitted), 1) AS avg_days,
               count(*) AS claims
        FROM fact_claims
        WHERE date_adjudicated IS NOT NULL AND date_submitted IS NOT NULL
        GROUP BY 1 ORDER BY avg_days DESC
    """)
    st.dataframe(by_status, width="stretch", hide_index=True)
    sql_expander(sql)


def q8() -> None:
    sql = """
        SELECT f.cpt_code, c.cpt_description, f.icd10_code, i.icd10_description,
               count(*) AS claims,
               count(*) FILTER (WHERE f.claim_status = 'Denied') AS denials,
               round(100.0 * count(*) FILTER (WHERE f.claim_status = 'Denied')
                     / count(*), 1) AS denial_rate_pct
        FROM fact_claims f
        JOIN dim_cpt   c ON f.cpt_code   = c.cpt_code
        JOIN dim_icd10 i ON f.icd10_code = i.icd10_code
        GROUP BY 1, 2, 3, 4
        HAVING count(*) >= :min_claims
        ORDER BY denial_rate_pct DESC
        LIMIT 10
    """
    df = show(QUESTIONS[7], sql, {"min_claims": MIN_PAIR_CLAIMS})
    top = df.iloc[0]
    st.markdown(
        f"**CPT {top.cpt_code} ({top.cpt_description}) with ICD-10 {top.icd10_code} "
        f"({top.icd10_description})** is denied most often — **{top.denial_rate_pct}%** "
        f"of {int(top.claims):,} claims."
    )
    st.dataframe(
        df, width="stretch", hide_index=True,
        column_config={
            "cpt_code": st.column_config.TextColumn("CPT"),
            "cpt_description": st.column_config.TextColumn("Procedure", width="medium"),
            "icd10_code": st.column_config.TextColumn("ICD-10"),
            "icd10_description": st.column_config.TextColumn("Diagnosis", width="medium"),
            "claims": st.column_config.NumberColumn("Claims", format="%d"),
            "denials": st.column_config.NumberColumn("Denials", format="%d"),
            "denial_rate_pct": st.column_config.ProgressColumn(
                "Denial rate", format="%.1f%%", min_value=0,
                max_value=float(df.denial_rate_pct.max())),
        })
    sql_expander(sql, f"Restricted to pairings with at least {MIN_PAIR_CLAIMS} claims. "
                      "Without that floor the ranking is dominated by pairings with a "
                      "handful of claims, where one denial swings the rate double digits.")


def q9() -> None:
    sql = """
        SELECT p.payer_type, count(*) AS claims,
               count(*) FILTER (WHERE f.claim_status = 'Denied') AS denials,
               round(100.0 * count(*) FILTER (WHERE f.claim_status = 'Denied')
                     / count(*), 2) AS denial_rate_pct
        FROM fact_claims f
        JOIN dim_payer p ON f.payer_id = p.payer_id
        WHERE p.payer_type IN ('Medicaid', 'Commercial')
        GROUP BY 1
        ORDER BY denial_rate_pct DESC
    """
    df = show(QUESTIONS[8], sql)
    d = df.set_index("payer_type").denial_rate_pct
    gap = abs(d["Medicaid"] - d["Commercial"])
    higher = "Medicaid" if d["Medicaid"] > d["Commercial"] else "Commercial"
    st.markdown(
        f"**Medicaid {d['Medicaid']}% vs Commercial {d['Commercial']}%** — "
        f"{higher} is higher, by {gap:.2f} points. On overall rate the two are "
        f"close; the meaningful difference is in *why* they deny."
    )
    st.plotly_chart(
        bar(df.sort_values("denial_rate_pct"), "denial_rate_pct", "payer_type",
            [SERIES[t] for t in df.sort_values("denial_rate_pct").payer_type],
            height=240),
        width="stretch")

    reasons = run_query("""
        SELECT p.payer_type, r.category,
               round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY p.payer_type), 1) AS share_pct
        FROM fact_claims f
        JOIN dim_payer p ON f.payer_id = p.payer_id
        JOIN dim_denial_reason r ON f.denial_reason_code = r.reason_code
        WHERE p.payer_type IN ('Medicaid', 'Commercial')
        GROUP BY 1, 2 ORDER BY 1, 3 DESC
    """)
    st.markdown("**Denial reasons differ sharply even though the rates don't:**")
    st.dataframe(reasons.pivot(index="category", columns="payer_type",
                               values="share_pct").fillna(0),
                 width="stretch")
    sql_expander(sql)


def q10() -> None:
    providers = run_query("""
        SELECT pr.provider_id, pr.provider_name
        FROM dim_provider pr ORDER BY pr.provider_id
    """)
    labels = {r.provider_id: f"{r.provider_id} — {r.provider_name}"
              for r in providers.itertuples()}
    st.subheader(QUESTIONS[9])
    choice = st.selectbox("Provider", providers.provider_id,
                          format_func=lambda p: labels[p],
                          index=int(providers.provider_id.tolist().index("PRV004")))
    sql = """
        SELECT date_trunc('quarter', f.date_of_service)::date AS quarter,
               count(*) AS claims,
               count(*) FILTER (WHERE f.claim_status = 'Denied') AS denials,
               round(100.0 * count(*) FILTER (WHERE f.claim_status = 'Denied')
                     / count(*), 2) AS denial_rate_pct
        FROM fact_claims f
        WHERE f.provider_id = :provider
        GROUP BY 1
        ORDER BY 1
    """
    df = run_query(sql, {"provider": choice})
    first, last = df.iloc[0], df.iloc[-1]
    delta = last.denial_rate_pct - first.denial_rate_pct
    verdict = ("risen" if delta > 1 else "fallen" if delta < -1 else "stayed flat")
    def q_label(ts) -> str:                    # strftime has no quarter code
        return f"{ts.year} Q{(ts.month - 1) // 3 + 1}"

    st.markdown(
        f"**{labels[choice]}**: denial rate has **{verdict}**, from "
        f"{first.denial_rate_pct}% in {q_label(first.quarter)} to "
        f"**{last.denial_rate_pct}%** in {q_label(last.quarter)} "
        f"({delta:+.1f} points across {len(df)} quarters)."
    )
    st.plotly_chart(
        line(df.quarter, df.denial_rate_pct,
             "<b>%{x|%Y-%m}</b><br>Denial rate: %{y:.2f}%"),
        width="stretch")
    st.dataframe(df, width="stretch", hide_index=True)
    sql_expander(sql)


ANSWERS = [q1, q2, q3, q4, q5, q6, q7, q8, q9, q10]


def render() -> None:
    st.title("Insights")
    st.caption(
        "The ten questions an analyst asks first — each answered by a reviewed SQL "
        "query against Supabase. Open the SQL panel under any answer to see exactly "
        "how it was calculated."
    )
    for tab, fn in zip(st.tabs(TAB_LABELS), ANSWERS):
        with tab:
            fn()
