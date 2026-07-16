"""
Scout — Gmail ingestion (Phase 1, step 2).

Reads Gmail for a single account (info@apexdigitalpicks.com by default) via
the Gmail API and OAuth, and writes into the same `contacts` /
`activity_log` columns the WhatsApp hook writes into (source='email').
Full message bodies stay in Gmail — only a short summary and a raw_ref
pointing at the Gmail message id are stored, per the spec.

SETUP (one-time, per the spec's OAuth-over-IMAP decision):
  1. In Google Cloud Console, create an OAuth 2.0 Client ID of type
     "Desktop app" for the Apex Digital Picks project. Download it as
     `gmail_client_secret.json` next to this file, or point
     GMAIL_CLIENT_SECRET_FILE at it.
  2. On a machine with a browser (NOT the headless server), run:
         python scout_gmail_ingest.py --authorize
     This opens a consent screen for info@apexdigitalpicks.com and writes
     `gmail_token.json`. Copy that file to the server's DATA_DIR (or
     wherever GMAIL_TOKEN_FILE points).
  3. First sync: run with --backfill, optionally scoped with --since/--until
     for a small test range first (per the spec's "test on your own inbox
     with a small date range first"), before running the full historical
     backfill. The full backfill is a one-off batch job chunked by month to
     respect Gmail API quotas — expect it to take a while.
  4. Ongoing sync: either wire up Pub/Sub push (`gmail_push_bp`) or call
     `poll_recent()` on a schedule (cron / APScheduler) — pick one, not
     both, or messages get double-logged (both paths dedupe on raw_ref, so
     double-logging is a wasted call, not a correctness bug, but avoid it).

Scope requested is read-only (gmail.readonly). Nothing is ever sent from
this account.
"""
import argparse
import base64
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import parseaddr

from flask import Blueprint, jsonify, request

from scout_common import find_db
from scout_contacts import get_or_create_by_email

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

GMAIL_CLIENT_SECRET_FILE = os.environ.get("GMAIL_CLIENT_SECRET_FILE", "gmail_client_secret.json")
GMAIL_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")
GMAIL_ACCOUNT = os.environ.get("GMAIL_ACCOUNT", "info@apexdigitalpicks.com")


# ---------- OAuth ----------

def get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        return creds

    raise RuntimeError(
        f"No valid Gmail credentials in {GMAIL_TOKEN_FILE}. Run "
        "`python scout_gmail_ingest.py --authorize` on a machine with a "
        "browser first, then copy the token file to this server."
    )


def authorize_interactive():
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(GMAIL_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"Authorized {GMAIL_ACCOUNT}. Wrote {GMAIL_TOKEN_FILE}. "
          "Copy this file to the server's DATA_DIR.")


def build_service():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)


# ---------- Sync watermark state ----------
# Not one of the spec's three tables — internal bookkeeping so polling and
# Pub/Sub push know where they left off. Created lazily so this module
# doesn't need its own migration step.

def _ensure_state_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS scout_sync_state (key TEXT PRIMARY KEY, value TEXT)"
    )


def _get_state(conn, key, default=None):
    _ensure_state_table(conn)
    row = conn.execute("SELECT value FROM scout_sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _set_state(conn, key, value):
    _ensure_state_table(conn)
    conn.execute(
        "INSERT INTO scout_sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------- Message parsing + ingestion ----------

def _parse_header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _parse_email_address(raw):
    name, addr = parseaddr(raw or "")
    return (addr or None), (name or None)


def _summarize(subject, snippet, limit=140):
    text = " ".join(filter(None, [subject, snippet]))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _already_logged(conn, raw_ref):
    return conn.execute(
        "SELECT 1 FROM activity_log WHERE raw_ref = ?", (raw_ref,)
    ).fetchone() is not None


def _ingest_message(conn, service, msg_id):
    raw_ref = f"gmail:{msg_id}"
    if _already_logged(conn, raw_ref):
        return

    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "To", "Subject"],
    ).execute()
    headers = msg.get("payload", {}).get("headers", [])
    from_addr, from_name = _parse_email_address(_parse_header(headers, "From"))
    to_addr, _to_name = _parse_email_address(_parse_header(headers, "To"))
    subject = _parse_header(headers, "Subject")
    snippet = msg.get("snippet", "")
    timestamp = int(msg.get("internalDate", "0")) / 1000.0

    if from_addr and from_addr.lower() == GMAIL_ACCOUNT.lower():
        direction = "outbound"
        counterpart_email, counterpart_name = to_addr, None
    else:
        direction = "inbound"
        counterpart_email, counterpart_name = from_addr, from_name

    if not counterpart_email:
        return  # can't resolve a contact from this header — skip rather than guess

    contact_id = get_or_create_by_email(conn, counterpart_email, counterpart_name)
    conn.execute(
        "INSERT INTO activity_log (contact_id, source, direction, timestamp, summary, raw_ref) "
        "VALUES (?, 'email', ?, ?, ?, ?)",
        (contact_id, direction, timestamp, _summarize(subject, snippet), raw_ref),
    )


def _list_with_backoff(service, **kwargs):
    from googleapiclient.errors import HttpError

    for attempt in range(6):
        try:
            return service.users().messages().list(userId="me", **kwargs).execute()
        except HttpError as e:
            if e.resp.status in (403, 429, 500, 503) and attempt < 5:
                wait = 2 ** attempt
                print(f"Gmail API {e.resp.status}, backing off {wait}s...")
                time.sleep(wait)
                continue
            raise


def _ingest_query(conn, service, query):
    page_token = None
    while True:
        resp = _list_with_backoff(service, q=query, pageToken=page_token, maxResults=100)
        for m in resp.get("messages", []):
            _ingest_message(conn, service, m["id"])
        conn.commit()
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def _add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    return dt.replace(year=year, month=month)


def backfill(db_path=None, start=None, end=None, chunk_months=1):
    service = build_service()
    conn = sqlite3.connect(db_path or find_db())
    try:
        start = start or datetime(2004, 4, 1, tzinfo=timezone.utc)  # Gmail's launch year
        end = end or datetime.now(timezone.utc)
        cursor = start
        while cursor < end:
            chunk_end = min(_add_months(cursor, chunk_months), end)
            query = f"after:{cursor:%Y/%m/%d} before:{chunk_end:%Y/%m/%d}"
            print(f"Backfilling {query} ...")
            _ingest_query(conn, service, query)
            cursor = chunk_end
    finally:
        conn.close()


def poll_recent(db_path=None):
    """Incremental sync using Gmail's history API. Call on a schedule, or
    from gmail_push_bp on a Pub/Sub push notification."""
    service = build_service()
    conn = sqlite3.connect(db_path or find_db())
    try:
        last_history_id = _get_state(conn, "gmail_last_history_id")
        if not last_history_id:
            profile = service.users().getProfile(userId="me").execute()
            _set_state(conn, "gmail_last_history_id", str(profile["historyId"]))
            conn.commit()
            print("No prior watermark — recorded current historyId. Run --backfill for history before this point.")
            return

        resp = service.users().history().list(
            userId="me", startHistoryId=last_history_id, historyTypes=["messageAdded"],
        ).execute()
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                _ingest_message(conn, service, added["message"]["id"])
        _set_state(conn, "gmail_last_history_id", str(resp.get("historyId", last_history_id)))
        conn.commit()
    finally:
        conn.close()


# ---------- Pub/Sub push endpoint ----------
# INTEGRATION: register like the other blueprints —
#   from scout_gmail_ingest import gmail_push_bp
#   app.register_blueprint(gmail_push_bp)
# and point the Gmail watch()'s Pub/Sub topic push subscription at
# POST /scout/gmail/push. Requires the Pub/Sub topic + watch() to be set
# up separately in Google Cloud Console — not automated here.

gmail_push_bp = Blueprint("scout_gmail_push", __name__, url_prefix="/scout/gmail")


@gmail_push_bp.route("/push", methods=["POST"])
def push():
    envelope = request.get_json(silent=True) or {}
    data = envelope.get("message", {}).get("data")
    if not data:
        return jsonify({"ok": False, "error": "no data"}), 400
    try:
        json.loads(base64.b64decode(data).decode("utf-8"))  # {emailAddress, historyId} — informational only
        poll_recent()
    except Exception as e:
        print(f"Gmail push sync failed: {e}")
        return jsonify({"ok": False}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize", action="store_true", help="Run the interactive OAuth flow")
    parser.add_argument("--backfill", action="store_true", help="Run the chunked historical backfill")
    parser.add_argument("--poll", action="store_true", help="Run one incremental sync")
    parser.add_argument("--since", help="Backfill start date, YYYY-MM-DD")
    parser.add_argument("--until", help="Backfill end date, YYYY-MM-DD")
    args = parser.parse_args()

    if args.authorize:
        authorize_interactive()
    elif args.backfill:
        start = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
        end = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc) if args.until else None
        backfill(start=start, end=end)
    elif args.poll:
        poll_recent()
    else:
        parser.print_help()
