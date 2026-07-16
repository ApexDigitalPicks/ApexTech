"""
Scout — verification view (Phase 1).

Per the spec: "no dashboard changes beyond confirming data is landing
correctly (a simple query/count view is enough for verification)." This is
deliberately minimal — a manual review tool for the week-long review
period, not a real dashboard page.

INTEGRATION (same pattern as dashboard_api.py):

    try:
        from scout_verify import scout_verify_bp
        app.register_blueprint(scout_verify_bp)
    except Exception as e:
        print(f"Scout verify view not loaded: {e}")
"""
import os
import sqlite3
import time
from functools import wraps

from flask import Blueprint, jsonify, request

from scout_common import find_db

scout_verify_bp = Blueprint("scout_verify", __name__, url_prefix="/scout")

ACCESS_KEY = os.environ.get("DASHBOARD_ACCESS_KEY", "")


def require_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ACCESS_KEY:
            return jsonify({"error": "Not configured. Set DASHBOARD_ACCESS_KEY."}), 503
        if request.headers.get("Authorization", "") != f"Bearer {ACCESS_KEY}":
            return jsonify({"error": "Invalid access key"}), 401
        return f(*args, **kwargs)
    return wrapper


def _db():
    conn = sqlite3.connect(find_db())
    conn.row_factory = sqlite3.Row
    return conn


@scout_verify_bp.route("/api/verify/summary")
@require_key
def summary():
    db = _db()
    try:
        def one(sql, args=()):
            return db.execute(sql, args).fetchone()[0]

        day_ago = time.time() - 86400
        payload = {
            "contacts_total": one("SELECT COUNT(*) FROM contacts"),
            "contacts_with_email": one("SELECT COUNT(*) FROM contacts WHERE email_address IS NOT NULL"),
            "contacts_with_whatsapp": one("SELECT COUNT(*) FROM contacts WHERE wa_id IS NOT NULL"),
            "activity_log_total": one("SELECT COUNT(*) FROM activity_log"),
            "activity_log_last_24h": one(
                "SELECT COUNT(*) FROM activity_log WHERE timestamp >= ?", (day_ago,)),
            "activity_by_source": {
                row["source"]: row["n"]
                for row in db.execute(
                    "SELECT source, COUNT(*) AS n FROM activity_log GROUP BY source"
                ).fetchall()
            },
            "notes_total": one("SELECT COUNT(*) FROM notes"),
        }
    finally:
        db.close()
    return jsonify(payload)


@scout_verify_bp.route("/api/verify/recent")
@require_key
def recent():
    limit = min(int(request.args.get("limit", 25)), 200)
    db = _db()
    try:
        rows = db.execute(
            """
            SELECT a.id, a.source, a.direction, a.timestamp, a.summary, a.raw_ref,
                   COALESCE(c.name, c.email_address, c.wa_id, '(unknown)') AS contact
            FROM activity_log a
            LEFT JOIN contacts c ON c.rowid = a.contact_id
            ORDER BY a.timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])
