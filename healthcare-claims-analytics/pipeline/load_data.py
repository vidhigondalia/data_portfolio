"""Load the generated CSVs into the Supabase Postgres tables.

    python pipeline/load_data.py                # load into empty tables
    python pipeline/load_data.py --truncate     # clear tables first, then load
    python pipeline/load_data.py --check-only   # run quality checks, load nothing

Dimensions load before fact_claims so foreign keys resolve. Rows failing a
quality check are dropped, not loaded, and written to output/_rejected/ for
inspection; the rest of the file still loads. The whole load runs in one
transaction, so a mid-load failure leaves the database untouched.

Connection details come from SUPABASE_DB_URL in .env (see create_schema.py).
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# Both scripts live in pipeline/, so this resolves when run as a script.
from create_schema import get_database_url, normalise_url, redact

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_ROOT / "data_generation" / "output"

# Load order matters: every table's foreign key targets must already be in.
LOAD_ORDER = [
    "dim_payer",
    "dim_provider",
    "dim_cpt",
    "dim_icd10",
    "dim_denial_reason",
    "dim_date",
    "fact_claims",
]

# Primary key of each dimension, used for null/duplicate checks and FK lookups.
DIM_KEYS = {
    "dim_payer": "payer_id",
    "dim_provider": "provider_id",
    "dim_cpt": "cpt_code",
    "dim_icd10": "icd10_code",
    "dim_denial_reason": "reason_code",
    "dim_date": "full_date",
}

# fact_claims column -> (dimension table, dimension key)
FACT_FOREIGN_KEYS = {
    "provider_id": ("dim_provider", "provider_id"),
    "payer_id": ("dim_payer", "payer_id"),
    "cpt_code": ("dim_cpt", "cpt_code"),
    "icd10_code": ("dim_icd10", "icd10_code"),
    "denial_reason_code": ("dim_denial_reason", "reason_code"),
    "date_of_service": ("dim_date", "full_date"),
}

MONEY_COLUMNS = ["billed_amount", "allowed_amount", "paid_amount"]


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------

def display_path(path: Path) -> str:
    """Project-relative when possible, absolute otherwise (e.g. a --input elsewhere)."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def check_dimension(name: str, df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Drop dimension rows with a null or duplicate primary key."""
    key = DIM_KEYS[name]
    notes: list[str] = []

    null_key = df[key].isna()
    if null_key.any():
        notes.append(f"{null_key.sum()} row(s) with null {key}")

    dupes = df[key].duplicated(keep="first") & ~null_key
    if dupes.any():
        notes.append(f"{dupes.sum()} duplicate {key} value(s)")

    bad = null_key | dupes
    return df[~bad].copy(), notes


def check_facts(df: pd.DataFrame, dims: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Return (clean, rejected, notes) for fact_claims.

    Rejects rows with a null/duplicate claim_id, an orphaned foreign key, or a
    negative dollar amount. A row can fail several checks; reject_reason lists
    every one so the CSV explains itself.
    """
    reasons = pd.Series([""] * len(df), index=df.index)

    def flag(mask: pd.Series, label: str) -> None:
        reasons[mask] = (reasons[mask] + "; " + label).str.lstrip("; ")

    notes: list[str] = []

    null_id = df["claim_id"].isna()
    if null_id.any():
        flag(null_id, "null claim_id")
        notes.append(f"{null_id.sum()} null claim_id")

    dupe_id = df["claim_id"].duplicated(keep="first") & ~null_id
    if dupe_id.any():
        flag(dupe_id, "duplicate claim_id")
        notes.append(f"{dupe_id.sum()} duplicate claim_id")

    for col, (dim_table, dim_key) in FACT_FOREIGN_KEYS.items():
        valid = set(dims[dim_table][dim_key].astype(str))
        # A null FK is allowed by the schema (e.g. denial_reason_code on a paid
        # claim); only non-null values that miss the dimension are orphans.
        present = df[col].notna()
        orphan = present & ~df[col].astype(str).isin(valid)
        if orphan.any():
            flag(orphan, f"orphaned {col}")
            notes.append(f"{orphan.sum()} orphaned {col} -> {dim_table}")

    for col in MONEY_COLUMNS:
        negative = pd.to_numeric(df[col], errors="coerce") < 0
        if negative.any():
            flag(negative, f"negative {col}")
            notes.append(f"{negative.sum()} negative {col}")

    bad = reasons != ""
    rejected = df[bad].copy()
    rejected["reject_reason"] = reasons[bad]
    return df[~bad].copy(), rejected, notes


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def copy_into(cursor, table: str, df: pd.DataFrame) -> int:
    """Bulk-insert a frame with COPY, far faster than row-by-row INSERT."""
    if df.empty:
        return 0
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    cols = ", ".join(f'"{c}"' for c in df.columns)
    cursor.copy_expert(
        f'COPY {table} ({cols}) FROM STDIN WITH (FORMAT CSV, NULL \'\')', buf
    )
    return len(df)


def existing_counts(conn) -> dict[str, int]:
    return {
        t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
        for t in LOAD_ORDER
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=INPUT_DIR)
    parser.add_argument("--truncate", action="store_true",
                        help="empty the tables before loading (destroys existing rows)")
    parser.add_argument("--check-only", action="store_true",
                        help="run quality checks and exit without writing")
    args = parser.parse_args()

    # --- read ------------------------------------------------------------
    frames: dict[str, pd.DataFrame] = {}
    for table in LOAD_ORDER:
        path = args.input / f"{table}.csv"
        if not path.is_file():
            sys.exit(f"Missing CSV: {path}\nRun: python data_generation/generate_data.py")
        frames[table] = pd.read_csv(path)

    # --- quality checks ---------------------------------------------------
    print("Quality checks")
    clean: dict[str, pd.DataFrame] = {}
    rejected_counts: dict[str, int] = {}
    all_notes: list[str] = []

    for table in LOAD_ORDER:
        if table == "fact_claims":
            continue
        kept, notes = check_dimension(table, frames[table])
        clean[table] = kept
        rejected_counts[table] = len(frames[table]) - len(kept)
        all_notes += [f"{table}: {n}" for n in notes]

    facts_clean, facts_rejected, fact_notes = check_facts(frames["fact_claims"], clean)
    clean["fact_claims"] = facts_clean
    rejected_counts["fact_claims"] = len(facts_rejected)
    all_notes += [f"fact_claims: {n}" for n in fact_notes]

    if all_notes:
        for note in all_notes:
            print(f"  REJECT  {note}")
        if not facts_rejected.empty:
            # Sit alongside the CSVs actually read, not the default location.
            reject_dir = args.input / "_rejected"
            reject_dir.mkdir(parents=True, exist_ok=True)
            out = reject_dir / "fact_claims.csv"
            facts_rejected.to_csv(out, index=False)
            print(f"  rejected rows written to {display_path(out)}")
    else:
        print("  no issues found — null claim_id, orphaned FKs, negative amounts all clear")

    if args.check_only:
        print("\n--check-only: nothing loaded.")
        return

    # --- load -------------------------------------------------------------
    raw_url, source = get_database_url()
    url = normalise_url(raw_url)
    print(f"\nconnection: {redact(url)}  (from {source})")

    engine = create_engine(url, future=True)
    loaded: dict[str, int] = {}
    try:
        with engine.connect() as conn:
            before = existing_counts(conn)
            conn.rollback()

            occupied = {t: n for t, n in before.items() if n}
            if occupied and not args.truncate:
                sys.exit(
                    "Tables already contain rows: "
                    + ", ".join(f"{t}={n}" for t, n in occupied.items())
                    + "\nRe-run with --truncate to replace them."
                )

            with conn.begin():
                cursor = conn.connection.cursor()
                if args.truncate:
                    # CASCADE because fact_claims references every dimension.
                    conn.exec_driver_sql(
                        f"TRUNCATE {', '.join(LOAD_ORDER)} RESTART IDENTITY CASCADE"
                    )
                    print("  truncated existing rows")
                for table in LOAD_ORDER:
                    loaded[table] = copy_into(cursor, table, clean[table])

            after = existing_counts(conn)

    except SQLAlchemyError as exc:
        sys.stdout.flush()
        print(f"\nLoad failed, transaction rolled back: {getattr(exc, 'orig', exc)}",
              file=sys.stderr)
        sys.exit(1)
    finally:
        engine.dispose()

    # --- summary ----------------------------------------------------------
    print(f"\n{'table':<20}{'in csv':>9}{'rejected':>10}{'loaded':>9}{'in db':>9}")
    print("-" * 57)
    for table in LOAD_ORDER:
        print(f"{table:<20}{len(frames[table]):>9,}{rejected_counts[table]:>10,}"
              f"{loaded[table]:>9,}{after[table]:>9,}")
    print("-" * 57)
    print(f"{'total':<20}{sum(len(f) for f in frames.values()):>9,}"
          f"{sum(rejected_counts.values()):>10,}{sum(loaded.values()):>9,}"
          f"{sum(after.values()):>9,}")


if __name__ == "__main__":
    main()
