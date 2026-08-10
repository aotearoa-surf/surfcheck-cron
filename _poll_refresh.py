# -*- coding: utf-8 -*-
"""MetOcean refresh telemetry: log fixed-future forecast values every run;
a value change means a new model run published. 1 API unit per poll.

Feeds the run-time tuning question (Che 10 Aug: "keep polling freshness
times, we may change again"). Analyse with:
  select polled_at at time zone 'Pacific/Auckland', changed
  from mo_refresh_log where changed order by polled_at desc;
"""
import json, os, urllib.request
from datetime import datetime, timedelta, timezone

MO_URL = "https://forecast-v2.metoceanapi.com/point/time"
POINT = {"lon": 174.653, "lat": -36.166}   # Forestry lineup


def sb(path, method="GET", body=None):
    req = urllib.request.Request(
        os.environ["SUPABASE_URL"] + "/rest/v1/" + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"apikey": os.environ["SUPABASE_SERVICE_KEY"],
                 "Authorization": "Bearer " + os.environ["SUPABASE_SERVICE_KEY"],
                 "Content-Type": "application/json", "Prefer": "return=minimal"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "null")


def main():
    # fixed targets 2/4/6 days out, midnight UTC: revised by every model run
    base = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    frm = (base + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = json.dumps({"points": [POINT], "variables": ["wave.height"],
                       "time": {"from": frm, "interval": "48h", "repeat": 2}}).encode()
    req = urllib.request.Request(MO_URL, data=body, headers={
        "Content-Type": "application/json", "x-api-key": os.environ["METOCEAN_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    vals = {t: round(v, 4) for t, v in zip(d["dimensions"]["time"]["data"],
                                           d["variables"]["wave.height"]["data"])
            if v is not None}
    prev = sb("mo_refresh_log?select=vals&order=polled_at.desc&limit=1")
    changed = bool(prev) and any(
        prev[0]["vals"].get(k) is not None and prev[0]["vals"].get(k) != v
        for k, v in vals.items())
    sb("mo_refresh_log", "POST", {"vals": vals, "changed": changed})
    print(f"poll ok, changed={changed}")


if __name__ == "__main__":
    main()
