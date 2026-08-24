"""Ask the Data: natural-language questions answered by generated SQL.

Flow per question:
  1. build a schema context string from the live database + business definitions
  2. ask Claude for a single read-only SELECT
  3. validate it is SELECT-only and touches only known tables
  4. execute in a read-only transaction with a row limit and statement timeout
  5. on error, feed the error back to the model once and retry
  6. send the rows back to the model for a plain-English answer

Every question, the SQL it produced, and the outcome are logged to the
query_log table (falling back to a local JSONL file if that write fails).

The Anthropic API key comes from st.secrets["ANTHROPIC_API_KEY"].
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st
from sqlalchemy import text

from db import KNOWN_TABLES, PROJECT_ROOT, get_engine, run_query

MODEL = "claude-sonnet-4-6"
ROW_LIMIT = 500                 # rows returned to the page
ROWS_TO_MODEL = 50              # rows shown to the model when writing the answer
STATEMENT_TIMEOUT_MS = 15_000
LOG_FALLBACK = PROJECT_ROOT / "app" / "logs" / "ask_data.jsonl"

# Semantics the schema cannot express. Without these the model invents its own
# definition of "denial rate" and silently answers a different question.
BUSINESS_RULES = """
Business definitions (follow these exactly):
- Denial rate = count of claims with claim_status = 'Denied' divided by total
  claims in the same population, expressed as a percentage. Use
  count(*) FILTER (WHERE claim_status = 'Denied') * 100.0 / count(*).
- claim_status is one of 'Paid', 'Denied', 'Partially Paid'. Only 'Denied'
  counts as a denial; 'Partially Paid' does not.
- A denial is preventable when its denial reason's category
  (dim_denial_reason.category) is one of 'Eligibility', 'Coding Error',
  'Authorization', 'Timely Filing'. Category 'Medical Necessity' is NOT
  preventable. The fact_claims.is_preventable column already encodes this.
- denial_reason_code and is_preventable are NULL for any claim that was not
  denied. Filter on claim_status = 'Denied' before aggregating them.
- billed_amount is what the provider charged; allowed_amount is what the payer
  approved; paid_amount is what was actually paid. Denied claims have
  allowed_amount = 0 and paid_amount = 0, so "lost revenue" from denials is
  best measured with billed_amount.
- Join claims to dates with fact_claims.date_of_service = dim_date.full_date.
- fact_claims is one row per claim. There is no claim-line detail.
"""

SYSTEM_PROMPT = """You translate questions about a healthcare claims database \
into PostgreSQL queries.

Rules:
- Return ONE read-only SELECT statement and nothing else.
- No INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, GRANT, COPY or any
  other statement that writes or changes state.
- No semicolons, no multiple statements, no comments.
- Reference only the tables listed in the schema.
- Prefer explicit JOINs and readable column aliases.
- Round percentages to one decimal place.
- Do not add a LIMIT unless the question asks for a top/bottom N; the caller
  applies its own row cap.
- Output the raw SQL only. No prose, no markdown fences, no explanation."""


# ---------------------------------------------------------------------------
# Schema context
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def schema_context() -> str:
    """Describe the live schema so the model never guesses column names."""
    cols = run_query("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ANY(:tables)
        ORDER BY table_name, ordinal_position
    """, {"tables": sorted(KNOWN_TABLES)})

    fks = run_query("""
        SELECT tc.table_name, kcu.column_name,
               ccu.table_name AS ref_table, ccu.column_name AS ref_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
        ORDER BY 1, 2
    """)

    parts = ["Tables and columns:"]
    for table, grp in cols.groupby("table_name"):
        fields = ", ".join(f"{r.column_name} {r.data_type}" for r in grp.itertuples())
        parts.append(f"  {table}({fields})")

    parts.append("\nRelationships:")
    for r in fks.itertuples():
        parts.append(f"  {r.table_name}.{r.column_name} -> {r.ref_table}.{r.ref_column}")

    # Small enumerations matter more than row counts for query correctness.
    for table, col in (("dim_payer", "payer_type"), ("dim_payer", "plan_type"),
                       ("dim_cpt", "category"), ("dim_denial_reason", "category"),
                       ("fact_claims", "claim_status")):
        vals = run_query(f"SELECT DISTINCT {col} AS v FROM {table} WHERE {col} IS NOT NULL ORDER BY 1")
        parts.append(f"  {table}.{col} values: {', '.join(map(str, vals.v))}")

    rng = run_query("SELECT min(full_date) lo, max(full_date) hi FROM dim_date")
    parts.append(f"\nDate range: {rng.lo[0]} to {rng.hi[0]}")
    parts.append(BUSINESS_RULES)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------

@st.cache_resource
def get_client() -> anthropic.Anthropic:
    try:
        key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        st.error(
            "No Anthropic API key configured. Add `ANTHROPIC_API_KEY` to "
            "`.streamlit/secrets.toml` (see `.streamlit/secrets.toml.example`)."
        )
        st.stop()
    return anthropic.Anthropic(api_key=key)


def _text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()


def strip_fences(sql: str) -> str:
    """Models often wrap SQL in ```sql fences despite being told not to."""
    fenced = re.search(r"```(?:sql)?\s*(.+?)\s*```", sql, re.S | re.I)
    if fenced:
        sql = fenced.group(1)
    return sql.strip().rstrip(";").strip()


def generate_sql(question: str, ctx: str, prior_sql: str = "", error: str = "") -> str:
    """Ask for SQL. When error is set this is the single retry."""
    if error:
        user = (f"{ctx}\n\nQuestion: {question}\n\n"
                f"Your previous query failed.\n\nQuery:\n{prior_sql}\n\n"
                f"Error:\n{error}\n\nReturn a corrected query. SQL only.")
    else:
        user = f"{ctx}\n\nQuestion: {question}\n\nSQL only."

    response = get_client().messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},   # correctness matters more than latency here
        messages=[{"role": "user", "content": user}],
    )
    return strip_fences(_text_of(response))


def explain_results(question: str, sql: str, df: pd.DataFrame) -> str:
    shown = df.head(ROWS_TO_MODEL)
    truncated = "" if len(df) <= ROWS_TO_MODEL else f"\n({len(df):,} rows total; first {ROWS_TO_MODEL} shown.)"
    user = (f"Question: {question}\n\nQuery run:\n{sql}\n\n"
            f"Results (CSV):\n{shown.to_csv(index=False)}{truncated}\n\n"
            "Answer the question in plain English for a non-technical reader. "
            "Lead with the direct answer, cite the specific numbers, and note "
            "anything that qualifies them. Do not describe the SQL. Be brief.")

    response = get_client().messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": user}],
    )
    return _text_of(response)


# ---------------------------------------------------------------------------
# Validation and execution
# ---------------------------------------------------------------------------

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|"
    r"vacuum|analyze|merge|call|do|execute|prepare|listen|notify|refresh|"
    r"comment|reindex|cluster|lock|set|reset|begin|commit|rollback|savepoint|"
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|pg_sleep|dblink|lo_import|"
    r"lo_export|pg_terminate_backend)\b", re.I)

TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.I)
CTE_NAME = re.compile(r"\b(?:with|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", re.I)


def validate_sql(sql: str) -> tuple[bool, str]:
    """Reject anything that is not a single read-only SELECT over known tables."""
    if not sql:
        return False, "the model returned nothing"
    if ";" in sql:
        return False, "contains a semicolon (multiple statements are not allowed)"
    if not re.match(r"^\s*(select|with)\b", sql, re.I):
        return False, "does not begin with SELECT or WITH"
    if "--" in sql or "/*" in sql:
        return False, "contains a SQL comment"

    hit = FORBIDDEN.search(sql)
    if hit:
        return False, f"contains the disallowed keyword '{hit.group(1).upper()}'"

    allowed = KNOWN_TABLES | {m.lower() for m in CTE_NAME.findall(sql)}
    referenced = {m.lower() for m in TABLE_REF.findall(sql)}
    unknown = referenced - allowed
    if unknown:
        return False, f"references unknown table(s): {', '.join(sorted(unknown))}"
    if not referenced:
        return False, "does not reference any table"
    return True, ""


def execute_sql(sql: str) -> pd.DataFrame:
    """Run inside a read-only transaction with a timeout and a hard row cap."""
    wrapped = f"SELECT * FROM (\n{sql}\n) AS _q LIMIT {ROW_LIMIT}"
    conn = get_engine().connect().execution_options(postgresql_readonly=True)
    try:
        with conn.begin():
            conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            return pd.read_sql_query(text(wrapped), conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

@st.cache_resource
def ensure_log_table() -> bool:
    try:
        with get_engine().begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS query_log (
                    id            BIGSERIAL PRIMARY KEY,
                    asked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    question      TEXT NOT NULL,
                    generated_sql TEXT,
                    attempts      SMALLINT,
                    status        TEXT NOT NULL,
                    detail        TEXT,
                    row_count     INTEGER,
                    duration_ms   INTEGER
                )
            """))
        return True
    except Exception:
        return False


def log_interaction(**row) -> None:
    """Log to query_log; fall back to a JSONL file if the database write fails."""
    if ensure_log_table():
        try:
            with get_engine().begin() as conn:
                conn.execute(text("""
                    INSERT INTO query_log
                        (question, generated_sql, attempts, status, detail, row_count, duration_ms)
                    VALUES (:question, :generated_sql, :attempts, :status, :detail,
                            :row_count, :duration_ms)
                """), row)
            return
        except Exception:
            pass
    try:
        LOG_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FALLBACK.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"asked_at": datetime.now(timezone.utc).isoformat(), **row}) + "\n")
    except Exception:
        pass        # logging must never break the page


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------

def answer(question: str) -> dict:
    """Returns {answer, sql, df, error, attempts}."""
    started = time.perf_counter()
    ctx = schema_context()
    out: dict = {"answer": "", "sql": "", "df": None, "error": "", "attempts": 0}
    prior_sql, prior_err = "", ""

    for attempt in (1, 2):                       # one generation, one retry
        out["attempts"] = attempt
        try:
            sql = generate_sql(question, ctx, prior_sql, prior_err)
        except anthropic.APIStatusError as exc:
            out["error"] = f"Anthropic API error: {exc.message}"
            break
        except anthropic.APIConnectionError:
            out["error"] = "Could not reach the Anthropic API. Check your connection."
            break
        out["sql"] = sql

        ok, why = validate_sql(sql)
        if not ok:
            prior_sql, prior_err = sql, f"The query was rejected by a safety check: {why}."
            out["error"] = f"Generated SQL rejected: {why}"
            continue                              # let the retry fix it

        try:
            out["df"] = execute_sql(sql)
            out["error"] = ""
            break
        except Exception as exc:
            prior_sql, prior_err = sql, str(getattr(exc, "orig", exc))[:1500]
            out["error"] = f"Query failed: {prior_err}"

    if out["df"] is not None:
        try:
            out["answer"] = explain_results(question, out["sql"], out["df"])
        except Exception as exc:
            out["answer"] = f"(Could not summarise the results: {exc})"

    log_interaction(
        question=question, generated_sql=out["sql"] or None, attempts=out["attempts"],
        status="ok" if out["df"] is not None else "error",
        detail=out["error"] or None,
        row_count=None if out["df"] is None else len(out["df"]),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return out


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

EXAMPLES = [
    "Which payer denies the most claims?",
    "What share of denials were preventable, by payer type?",
    "Which providers had a rising denial rate in 2025?",
    "How much billed revenue did we lose to authorization denials?",
]


def render() -> None:
    st.title("Ask the Data")
    st.caption(
        f"Questions are turned into a read-only SQL query by {MODEL}, run against "
        "Supabase, and summarised. Every question and query is logged."
    )

    if "chat" not in st.session_state:
        st.session_state.chat = []

    if not st.session_state.chat:
        st.markdown("**Try one of these:**")
        cols = st.columns(len(EXAMPLES))
        for col, example in zip(cols, EXAMPLES):
            if col.button(example, width="stretch"):
                st.session_state.pending = example
                st.rerun()

    for turn in st.session_state.chat:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("sql"):
                with st.expander("SQL"):
                    st.code(turn["sql"], language="sql")
            if turn.get("df") is not None:
                st.dataframe(turn["df"], width="stretch", hide_index=True)

    question = st.chat_input("Ask a question about the claims data…")
    if not question:
        question = st.session_state.pop("pending", None)
    if not question:
        return

    st.session_state.chat.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Writing SQL and querying…"):
            result = answer(question)

        if result["df"] is None:
            msg = f"I couldn't answer that. {result['error']}"
            st.error(msg)
            if result["sql"]:
                with st.expander("SQL"):
                    st.code(result["sql"], language="sql")
            st.session_state.chat.append(
                {"role": "assistant", "content": msg, "sql": result["sql"]})
            return

        st.markdown(result["answer"])
        if result["attempts"] > 1:
            st.caption("The first query failed; this is the corrected one.")
        with st.expander("SQL"):
            st.code(result["sql"], language="sql")
        st.dataframe(result["df"], width="stretch", hide_index=True)
        if len(result["df"]) == ROW_LIMIT:
            st.caption(f"Showing the first {ROW_LIMIT} rows.")

        st.session_state.chat.append({
            "role": "assistant", "content": result["answer"],
            "sql": result["sql"], "df": result["df"],
        })
