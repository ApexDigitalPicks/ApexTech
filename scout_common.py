"""
Scout — shared helpers.

DB discovery mirrors dashboard_api.py's convention so both blueprints find
the same SQLite file without needing separate configuration.
"""
import glob
import os


def find_db():
    explicit = os.environ.get("APEXFLOW_DB_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    data_dir = os.environ.get(
        "DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    )
    hits = sorted(glob.glob(os.path.join(data_dir, "*.db")))
    if hits:
        return hits[0]
    raise FileNotFoundError("No SQLite database found. Set APEXFLOW_DB_PATH.")
