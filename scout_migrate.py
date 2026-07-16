"""
Scout Phase 1 — database migration.

Adds Scout's data-logging tables to the existing ApexFlow SQLite database.

IMPORTANT — schema collision handled deliberately:
ApexFlow already has a `contacts` table (wa_id, name, language,
consent_sent, opted_out, created_at) used by the dashboard and the
WhatsApp bot (see dashboard_api.py's docstring). The Phase 1 spec's
`contacts` table is NOT created fresh — that would collide with and shadow
the existing one. Instead this migration extends the existing table with
the columns Scout needs, reuses its rowid as the numeric `id` every FK in
the spec expects, and reuses the existing `name` column as the spec's
`display_name`.

`activity_log` and `notes` don't exist yet in ApexFlow, so those are
created as specified.

Dry-run by default. Run with --apply only after checking the plan against
a copy of the production DB, per the existing pre-flight test standard —
never point this at the live container's DB on the first run.
"""
import argparse
import sqlite3

from scout_common import find_db

NEW_CONTACT_COLUMNS = [
    ("email_address", "TEXT"),
    ("business_name", "TEXT"),
    ("last_activity_at", "REAL"),
    ("merged_into", "INTEGER REFERENCES contacts(rowid)"),
]

ACTIVITY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(rowid),
    source TEXT NOT NULL CHECK (source IN ('whatsapp', 'email')),
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    timestamp REAL NOT NULL,
    summary TEXT,
    raw_ref TEXT
)
""".strip()

ACTIVITY_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_activity_log_contact ON activity_log(contact_id)",
    "CREATE INDEX IF NOT EXISTS idx_activity_log_timestamp ON activity_log(timestamp)",
]

NOTES_DDL = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(rowid),
    tag TEXT,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'voice', 'auto'))
)
""".strip()


def _existing_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def plan(conn):
    """Return the list of (description, sql) steps this migration would run."""
    steps = []

    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "contacts" not in tables:
        raise RuntimeError(
            "No existing `contacts` table found. This migration assumes the "
            "ApexFlow schema documented in dashboard_api.py already exists — "
            "aborting rather than guessing at one."
        )

    existing_cols = _existing_columns(conn, "contacts")
    for col, coltype in NEW_CONTACT_COLUMNS:
        if col not in existing_cols:
            steps.append((
                f"Add contacts.{col}",
                f"ALTER TABLE contacts ADD COLUMN {col} {coltype}",
            ))

    if "activity_log" not in tables:
        steps.append(("Create activity_log", ACTIVITY_LOG_DDL))
    for idx_sql in ACTIVITY_LOG_INDEXES:
        steps.append(("Ensure activity_log index", idx_sql))

    if "notes" not in tables:
        steps.append(("Create notes", NOTES_DDL))

    return steps


def run(db_path, dry_run=True):
    conn = sqlite3.connect(db_path)
    try:
        steps = plan(conn)
        if not steps:
            print("Schema already up to date. Nothing to do.")
            return

        print(f"{'DRY RUN — ' if dry_run else ''}{len(steps)} step(s) planned against {db_path}:")
        for desc, sql in steps:
            print(f"  - {desc}")
            print(f"      {sql}")

        if dry_run:
            print("\nDry run only. Re-run with --apply to execute against this DB.")
            return

        for _desc, sql in steps:
            conn.execute(sql)
        conn.commit()
        print("\nMigration applied.")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", nargs="?", default=None,
                         help="Path to the ApexFlow SQLite database (defaults to scout_common.find_db())")
    parser.add_argument("--apply", action="store_true",
                         help="Actually run the migration. Default is dry-run.")
    args = parser.parse_args()
    run(args.db_path or find_db(), dry_run=not args.apply)
