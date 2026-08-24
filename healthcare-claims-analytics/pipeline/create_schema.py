"""Execute sql/schema.sql against the Postgres database named in .env.

Usage:
    python pipeline/create_schema.py            # apply the schema
    python pipeline/create_schema.py --dry-run  # connect + print, change nothing
    python pipeline/create_schema.py --file sql/other.sql

Reads the connection string from SUPABASE_DB_URL, falling back to DATABASE_URL.
The whole file runs in a single transaction: if any statement fails, nothing is
left half-applied.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = PROJECT_ROOT / "sql" / "schema.sql"
ENV_VARS = ("SUPABASE_DB_URL", "DATABASE_URL")


def get_database_url() -> tuple[str, str]:
    """Return (url, source_var_name), or exit with an explanation."""
    load_dotenv(PROJECT_ROOT / ".env")

    for var in ENV_VARS:
        url = os.getenv(var)
        if url and url.strip():
            return url.strip(), var

    sys.exit(
        f"No connection string found. Set one of {' or '.join(ENV_VARS)} in "
        f"{PROJECT_ROOT / '.env'}\n"
        "  cp .env.example .env   # then fill in the real value"
    )


def normalise_url(url: str) -> str:
    """Pin SQLAlchemy to psycopg2 and require TLS for hosted Postgres."""
    if url.startswith("postgres://"):  # legacy scheme SQLAlchemy rejects
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    if "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    return url


def redact(url: str) -> str:
    """Mask the password so the URL is safe to print or log."""
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:****@", url)


def read_schema(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"Schema file not found: {path}")
    sql = path.read_text(encoding="utf-8")
    if not sql.strip():
        sys.exit(f"Schema file is empty: {path}")
    return sql


def apply_schema(url: str, sql: str, dry_run: bool) -> None:
    engine = create_engine(url, future=True)

    try:
        with engine.connect() as conn:
            server, db, user = conn.execute(
                text("SELECT version(), current_database(), current_user")
            ).one()
            print(f"  connected to : {db} as {user}")
            print(f"  server       : {server.split(',')[0]}")

            # The SELECT above autobegan a transaction; close it out so the
            # explicit begin() below is legal.
            conn.rollback()

            if dry_run:
                print("\n--dry-run: connection verified, no statements executed.")
                return

            # exec_driver_sql passes the file to psycopg2 verbatim, so multiple
            # statements and the file's own BEGIN/COMMIT work as written.
            with conn.begin():
                conn.exec_driver_sql(sql)

            tables = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
            ).scalars().all()

        print(f"\nSchema applied. {len(tables)} table(s) in public:")
        for name in tables:
            print(f"  - {name}")

    except SQLAlchemyError as exc:
        # SQLAlchemy wraps the driver error; the inner one is the useful part.
        detail = getattr(exc, "orig", exc)
        sys.stdout.flush()  # keep the context lines above the error
        print(f"\nFailed to apply schema: {detail}", file=sys.stderr)
        if "could not translate host name" in str(detail) or "Network is unreachable" in str(detail):
            print(
                "\nHint: Supabase direct connections (db.<ref>.supabase.co:5432) are\n"
                "IPv6-only on some networks. Use the pooler URL from the Supabase\n"
                "dashboard (Connect -> Session pooler) instead.",
                file=sys.stderr,
            )
        sys.exit(1)
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=DEFAULT_SCHEMA,
                        help=f"SQL file to execute (default: {DEFAULT_SCHEMA.relative_to(PROJECT_ROOT)})")
    parser.add_argument("--dry-run", action="store_true",
                        help="verify the connection and exit without executing")
    args = parser.parse_args()

    raw_url, source = get_database_url()
    url = normalise_url(raw_url)
    sql = read_schema(args.file)

    print(f"  schema file  : {args.file}")
    print(f"  connection   : {redact(url)}  (from {source})")

    apply_schema(url, sql, args.dry_run)


if __name__ == "__main__":
    main()
