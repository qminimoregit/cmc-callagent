#!/usr/bin/env python3
"""
fix_stuck_calls.py
──────────────────
One-time migration: marks all stuck 'in-progress' calls as 'completed'.

Run from the project root:
    uv run python scripts/fix_stuck_calls.py
"""

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import psycopg
from psycopg.rows import dict_row

DB_URL = os.environ["DATABASE_URL"]


def main():
    ended_at = datetime.now(timezone.utc).isoformat()

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # Check how many are stuck
            cur.execute("SELECT COUNT(*) as c FROM calls WHERE status = 'in-progress'")
            count = cur.fetchone()["c"]

            if count == 0:
                print("✅  No stuck calls — DB is already clean.")
                return

            print(f"Found {count} stuck call(s) — marking all as 'completed'...\n")

            # Update: preserve ended_at if already set, else use now()
            cur.execute(
                """UPDATE calls
                   SET status   = 'completed',
                       ended_at = COALESCE(ended_at, %s)
                   WHERE status = 'in-progress'
                   RETURNING call_sid, phone_number""",
                (ended_at,),
            )
            updated = cur.fetchall()

        conn.commit()

    print(f"✅  Fixed {len(updated)} call(s):")
    for r in updated:
        sid   = r["call_sid"]
        phone = r["phone_number"] or "(no number)"
        print(f"   • {sid} — {phone}")


if __name__ == "__main__":
    main()
