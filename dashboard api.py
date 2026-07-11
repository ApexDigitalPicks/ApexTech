"""
ApexFlow Client Dashboard API, v2.

Rewritten for the real ApexFlow schema:
    contacts  > wa_id, name, language, consent_sent, opted_out, created_at
    messages  > id, wa_id, direction, body, wa_message_id, created_at
    drafts    > id, wa_id, body, intent, status, meta, created_at, reviewed_at
    leads     > id, wa_id, fields, status, created_at, updated_at
    quotes    > id, quote_number, wa_id, lines, subtotal, vat, total,
                pdf_path, status, created_at

All created_at values are REAL unix timestamps.
Draft statuses: pending / approved / rejected / sent / failed.

Register in app.py with the crash-proof block (see INTEGRATION notes):

    try:
        from dashboard_api import dashboard_bp
        app.register_blueprint(dashboard_bp)
    except Exception as e:
        print(f"Dashboard not loaded: {e}")

Env vars:
    DASHBOARD_ACCESS_KEY   long random string given to the client
    DATA_DIR               already set by the app; the db file is found inside it
    APEXFLOW_DB_PATH       optional explicit path, overrides discovery
"""

import glob
import os
import sqlite3
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request, send_from_directory

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

ACCESS_KEY = os.environ.get("DASHBOARD_ACCESS_KEY", "")


def _find_db():
    explicit = os.environ.get("APEXFLOW_DB_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    hits = sorted(glob.glob(os.path.join(data_dir, "*.db")))
    if hits:
        return hits[0]
    raise FileNotFoundError("No SQLite database found. Set APEXFLOW_DB_PATH.")


def get_db():
    conn = sqlite3.connect(_find_db())
    conn.row_factory = sqlite3.Row
    return conn


def require_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ACCESS_KEY:
            return jsonify({"error": "Dashboard is not configured. Set DASHBOARD_ACCESS_KEY."}), 503
        if request.headers.get("Authorization", "") != f"Bearer {ACCESS_KEY}":
            return jsonify({"error": "Invalid access key"}), 401
        return f(*args, **kwargs)
    return wrapper


def _midnight_ts():
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


# ---------- Page ----------

@dashboard_bp.route("/")
def page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


# ---------- Auth ----------

@dashboard_bp.route("/api/auth", methods=["POST"])
def auth_check():
    data = request.get_json(silent=True) or {}
    if ACCESS_KEY and data.get("key") == ACCESS_KEY:
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401


# ---------- Stats ----------

@dashboard_bp.route("/api/stats")
@require_key
def stats():
    midnight = _midnight_ts()
    week_ago = time.time() - 7 * 86400
    db = get_db()
    try:
        def one(sql, args=()):
            return db.execute(sql, args).fetchone()[0]

        payload = {
            "messages_today": one(
                "SELECT COUNT(*) FROM messages WHERE created_at >= ?", (midnight,)),
            "pending_approvals": one(
                "SELECT COUNT(*) FROM drafts WHERE status = 'pending'"),
            "active_chats_week": one(
                "SELECT COUNT(DISTINCT wa_id) FROM messages WHERE created_at >= ?", (week_ago,)),
            "replies_sent_week": one(
                "SELECT COUNT(*) FROM drafts WHERE status IN ('approved','sent') AND reviewed_at >= ?", (week_ago,)),
            "open_leads": one(
                "SELECT COUNT(*) FROM leads WHERE status = 'open'"),
            "leads_won_week": one(
                "SELECT COUNT(*) FROM leads WHERE status = 'won' AND updated_at >= ?", (week_ago,)),
            "quotes_sent_week": one(
                "SELECT COUNT(*) FROM quotes WHERE status IN ('sent','accepted') AND created_at >= ?", (week_ago,)),
            "quote_value_week": one(
                "SELECT COALESCE(SUM(total),0) FROM quotes WHERE status IN ('sent','accepted') AND created_at >= ?", (week_ago,)),
        }
    finally:
        db.close()
    return jsonify(payload)


# ---------- Conversations ----------

@dashboard_bp.route("/api/conversations")
@require_key
def conversations():
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT m.wa_id AS id,
                   COALESCE(c.name, m.wa_id) AS name,
                   m.wa_id AS phone,
                   MAX(m.created_at) AS last_activity,
                   (SELECT body FROM messages m2
                    WHERE m2.wa_id = m.wa_id
                    ORDER BY m2.created_at DESC, m2.id DESC LIMIT 1) AS last_message
            FROM messages m
            LEFT JOIN contacts c ON c.wa_id = m.wa_id
            GROUP BY m.wa_id
            ORDER BY last_activity DESC
            LIMIT 50
            """
        ).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])


@dashboard_bp.route("/api/conversations/<wa_id>/messages")
@require_key
def conversation_messages(wa_id):
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT id,
                   CASE WHEN lower(direction) IN ('out','outbound','sent')
                        THEN 'out' ELSE 'in' END AS direction,
                   body,
                   created_at
            FROM messages
            WHERE wa_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 200
            """,
            (wa_id,),
        ).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])


# ---------- Approval queue ----------

@dashboard_bp.route("/api/queue")
@require_key
def queue():
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT d.id,
                   d.wa_id AS conversation_id,
                   d.body AS draft,
                   d.intent,
                   d.created_at,
                   COALESCE(c.name, d.wa_id) AS contact_name,
                   d.wa_id AS phone
            FROM drafts d
            LEFT JOIN contacts c ON c.wa_id = d.wa_id
            WHERE d.status = 'pending'
            ORDER BY d.created_at ASC
            """
        ).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])


def _decide(item_id, new_status):
    db = get_db()
    try:
        cur = db.execute(
            "UPDATE drafts SET status = ?, reviewed_at = ? WHERE id = ? AND status = 'pending'",
            (new_status, time.time(), item_id),
        )
        db.commit()
        changed = cur.rowcount
    finally:
        db.close()
    if changed == 0:
        return jsonify({"error": "Item not found or already decided"}), 409
    return jsonify({"ok": True, "id": item_id, "status": new_status})


@dashboard_bp.route("/api/queue/<int:item_id>/approve", methods=["POST"])
@require_key
def approve(item_id):
    # Only flips status to approved. The existing worker that watches for
    # approved drafts still does the actual WhatsApp send, so the approval
    # queue rule is never bypassed.
    return _decide(item_id, "approved")


@dashboard_bp.route("/api/queue/<int:item_id>/reject", methods=["POST"])
@require_key
def reject(item_id):
    return _decide(item_id, "rejected")
