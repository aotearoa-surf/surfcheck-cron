"""Prune slot_forecast: delete past slots older than KEEP_DAYS of history.
Keeps all current + future slots AND the last KEEP_DAYS of past slots (Che's call:
keep 7 days of history, just never query it). Self-contained; also called at the
end of _fetch_main.py each cycle so the table stays lean automatically.

Destructive DELETE, but scoped to old past slots only (no value beyond history).
"""
import os, sys, io
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv

KEEP_DAYS = 7

def _count(URL, H, params):
    r = requests.get(f"{URL}/rest/v1/slot_forecast",
                     headers={**H, "Prefer": "count=exact", "Range": "0-0"},
                     params={**params, "select": "spot_id"}, timeout=90)
    return r.headers.get("content-range", "*/?").split("/")[-1]

def prune_old_slots(keep_days=KEEP_DAYS, verbose=True):
    load_dotenv()
    URL = os.environ["SUPABASE_URL"].rstrip("/")
    KEY = os.environ["SUPABASE_SERVICE_KEY"]
    H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
    NZ = timezone(timedelta(hours=12))
    cutoff = (datetime.now(NZ).date() - timedelta(days=keep_days)).isoformat()  # YYYY-MM-DD
    before_total = _count(URL, H, {})
    to_delete = _count(URL, H, {"slot_key": f"lt.{cutoff}"})
    d = requests.delete(f"{URL}/rest/v1/slot_forecast",
                        headers={**H, "Prefer": "return=minimal"},
                        params={"slot_key": f"lt.{cutoff}"}, timeout=240)
    d.raise_for_status()
    after_total = _count(URL, H, {})
    if verbose:
        print(f"prune: keep {keep_days}d history (cutoff slot_key < {cutoff})")
        print(f"  table before: {before_total} rows")
        print(f"  deleted (slot_key < {cutoff}): {to_delete} rows")
        print(f"  table after:  {after_total} rows")
    return to_delete

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    prune_old_slots()
