"""
Scout — WhatsApp write-hook.

Call `log_whatsapp_activity(...)` from inside the existing ApexFlow
WhatsApp webhook handler, once per inbound or outbound message, after the
existing `messages` table insert. This does not replace or touch that
insert — it's a side write into Scout's own activity_log + contacts
columns (see scout_migrate.py).

INTEGRATION (manual — this file does not locate the handler for you):

    from scout_whatsapp_hook import log_whatsapp_activity

    # after existing message handling, per message:
    log_whatsapp_activity(
        db_path=APEXFLOW_DB_PATH,
        wa_id=wa_id,
        direction="inbound",  # or "outbound"
        body=message_body,
        display_name=profile_name,  # from the WhatsApp webhook payload if present, else None
        raw_ref=f"messages:{message_row_id}",
    )
"""
import sqlite3
import time

from scout_contacts import get_or_create_by_wa_id


def _summarize(body, limit=140):
    if not body:
        return ""
    body = " ".join(body.split())
    return body if len(body) <= limit else body[: limit - 1].rstrip() + "…"


def log_whatsapp_activity(db_path, wa_id, direction, body, display_name=None,
                           raw_ref=None, timestamp=None):
    if direction not in ("inbound", "outbound"):
        raise ValueError(f"direction must be 'inbound' or 'outbound', got {direction!r}")

    conn = sqlite3.connect(db_path)
    try:
        contact_id = get_or_create_by_wa_id(conn, wa_id, display_name)
        conn.execute(
            "INSERT INTO activity_log (contact_id, source, direction, timestamp, summary, raw_ref) "
            "VALUES (?, 'whatsapp', ?, ?, ?, ?)",
            (contact_id, direction, timestamp or time.time(), _summarize(body), raw_ref),
        )
        conn.commit()
    finally:
        conn.close()
