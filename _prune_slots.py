"""Archive + prune slot_forecast in one atomic server-side step.

Calls the Postgres RPC archive_and_prune_slots(keep_days): it copies every slot
older than (NZ today - keep_days) into the cold slot_history archive (idempotent
upsert), then deletes those same rows from the hot slot_forecast table - all in one
transaction, so a row is never deleted unless it was archived first.

Retention: keeps today + the trailing keep_days window LIVE. Today's already-passed
slots (12am, 6am...) stay in slot_forecast so the 7-day forecast grid can still show
how earlier today went - they only become eligible to archive once the whole day is
over and settled. The cutoff is today-minus-keep_days, so today is never selectable.

slot_history grows forever (cold, never read by the live site); migrate it off
Supabase (2nd project / R2 Parquet) when it nears the free tier. Called at the end
of _fetch_main.py each cycle. The cutoff logic lives in the RPC, so the root and the
surfcheck-cron copies of this file are identical and only need to call it.
"""
import os, sys, io
import requests
from dotenv import load_dotenv

KEEP_DAYS = 7

def prune_old_slots(keep_days=KEEP_DAYS, verbose=True):
    load_dotenv()
    URL = os.environ["SUPABASE_URL"].rstrip("/")
    KEY = os.environ["SUPABASE_SERVICE_KEY"]
    H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    r = requests.post(f"{URL}/rest/v1/rpc/archive_and_prune_slots",
                      headers=H, json={"keep_days": keep_days}, timeout=240)
    r.raise_for_status()
    pruned = r.json()  # scalar int: rows archived + removed this cycle
    if verbose:
        print(f"archive+prune: keep today + {keep_days}d live; "
              f"archived & removed {pruned} aged-out rows (now in slot_history)")
    return pruned

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    prune_old_slots()
