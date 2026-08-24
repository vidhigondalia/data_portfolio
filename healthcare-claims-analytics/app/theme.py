"""Shared chart palette and chrome.

The categorical slots are colorblind-validated in both light and dark modes;
assign them in fixed order and never cycle. Light mode's third slot sits below
3:1 contrast, so charts using it must carry direct labels or a table view.
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

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
