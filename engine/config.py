"""config.py - paths, policy loading and the database connection."""
import sqlite3
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "ap.db"


def load_policy() -> dict:
    """All thresholds live in config/policy.yaml so a buyer can tune them."""
    with open(ROOT / "config" / "policy.yaml") as f:
        return yaml.safe_load(f)


def connect(path=None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row  # rows behave like dicts: row["po_number"]
    return con
