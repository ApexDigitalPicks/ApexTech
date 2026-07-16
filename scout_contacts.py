"""
Scout — shared contact resolution.

Both ingestion sources (WhatsApp, email) upsert into the existing ApexFlow
`contacts` table rather than a fresh one — see scout_migrate.py for why.
This module is the one place that knows how to insert a new contacts row,
so it's the one place that has to cope with whatever NOT NULL columns the
live schema turns out to have beyond what Phase 1 added.
"""
import time

# Values to fill in for existing NOT NULL columns (e.g. ApexFlow's
# language/consent_sent/opted_out) when Scout creates a contact that didn't
# come through the WhatsApp consent flow (e.g. an email-only contact).
# Extend this if the live schema has other NOT NULL columns without
# defaults — the insert raises a clear error naming the column instead of
# guessing silently.
KNOWN_DEFAULTS = {
    "language": "en",
    "consent_sent": 0,
    "opted_out": 0,
}


def _contact_columns_info(conn):
    return {
        row[1]: {"notnull": bool(row[3]), "default": row[4]}
        for row in conn.execute("PRAGMA table_info(contacts)")
    }


def _insert_contact(conn, known_values):
    cols_info = _contact_columns_info(conn)
    values = dict(known_values)
    for col, info in cols_info.items():
        if col in values:
            continue
        if info["notnull"] and info["default"] is None:
            if col in KNOWN_DEFAULTS:
                values[col] = KNOWN_DEFAULTS[col]
            else:
                raise RuntimeError(
                    f"contacts.{col} is NOT NULL with no default and no known "
                    "value for a Scout-created contact. Add it to "
                    "scout_contacts.KNOWN_DEFAULTS."
                )
    cols = list(values)
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(values[c] for c in cols),
    )
    return cur.lastrowid


def get_or_create_by_wa_id(conn, wa_id, display_name=None):
    row = conn.execute("SELECT rowid FROM contacts WHERE wa_id = ?", (wa_id,)).fetchone()
    now = time.time()
    if row:
        contact_id = row[0]
        if display_name:
            conn.execute(
                "UPDATE contacts SET last_activity_at = ?, "
                "name = COALESCE(name, ?) WHERE rowid = ?",
                (now, display_name, contact_id),
            )
        else:
            conn.execute(
                "UPDATE contacts SET last_activity_at = ? WHERE rowid = ?",
                (now, contact_id),
            )
        return contact_id

    return _insert_contact(conn, {
        "wa_id": wa_id,
        "name": display_name,
        "created_at": now,
        "last_activity_at": now,
    })


def get_or_create_by_email(conn, email_address, display_name=None):
    row = conn.execute(
        "SELECT rowid FROM contacts WHERE email_address = ?", (email_address,)
    ).fetchone()
    now = time.time()
    if row:
        contact_id = row[0]
        conn.execute(
            "UPDATE contacts SET last_activity_at = ? WHERE rowid = ?",
            (now, contact_id),
        )
        return contact_id

    return _insert_contact(conn, {
        "email_address": email_address,
        "name": display_name,
        "created_at": now,
        "last_activity_at": now,
    })
