# -*- coding: utf-8 -*-
"""STAGE 1 of the MetOcean swap (M1). Writes slot_forecast_STAGING only.

Never touches slot_forecast. Safe to run by hand: zero Stormglass calls.
One run costs 124 spots x 7 variables = 868 MetOcean units.

Pipeline (deliberately SHORTER than _fetch_main.py):
  MetOcean nearshore at lineup GPS -> factor (shadow spots only) -> band split
  at 8s (native) -> rating (same compute_rating: weights, rideability, wave
  gate, ceilings, long-period lift).

DEAD under M1, on purpose, do not re-add: Tp x1.2 (MetOcean periods are peak),
energy swap + 30%/9s override + R5c (band split is native), offshore chop trim
(nearshore model already resolves what an offshore wind delivers to the beach).

Factors below were fitted on the two 9 Aug GSN captures (44 spots, lead 0-2).
Fits within 0.90-1.10 are set to 1.0 to avoid chasing noise. All other spots
1.0. Wed 12 Aug re-capture is the out-of-sample gate before any of this ships.
"""
import os, sys, json, time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for envfile in (ROOT / ".env", ROOT.parent / ".env"):
    if envfile.exists():
        for line in envfile.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from _fetch_main import (compute_rating, http_get, sb_select, sb_upsert_chunk,
                         nz_now, slot_key, NZ_TZ)

METOCEAN_KEY = os.environ.get("METOCEAN_KEY", "NCFRKCtuDqNsKxRmwBeZiD")  # trial
MO_URL = "https://forecast-v2.metoceanapi.com/point/time"
OM_KEY = os.environ["OPEN_METEO_KEY"]
DAYS = 10
STAGING_TABLE = "slot_forecast_staging"

MO_VARS = ["wave.height", "wave.period.peak",
           "wave.height.above-8s", "wave.period.above-8s.peak", "wave.direction.above-8s.peak",
           "wave.height.below-8s", "wave.period.below-8s.peak", "wave.direction.below-8s.peak"]
# wave.period.peak is the DISPLAY period: measured 1.17 s vs GSN against 1.82 s
# for a dominant-band-peak construction (validated on staging run 1, 9 Aug).

# fitted on gsn_2026-08-09.json + gsn_2026-08-09_batch2.json, threshold 0.90-1.10
M1_FACTORS = {
    "takapuna": 0.40, "omaha": 0.40, "okiwi-gbi": 0.45, "pauanui": 0.75,
    "sumner-scarborough": 0.45, "mahia-north-coast": 0.45,
    "hotwater-beach": 0.85, "waihi-beach": 0.85, "waipu-cove": 0.85, "matata": 1.15,
    "aramoana": 0.70, "cray-bay": 0.65, "gizzy-pipe": 1.20, "mahia-open-coast": 0.85,
    "meatworks": 1.20, "ocean-beach-hb": 0.75, "otama": 0.75,
    "raglan-tirohanga-indies": 0.85, "riverton": 0.65, "tauroa-shippies": 1.15,
    "te-awanga": 0.50, "the-cut-blenheim": 0.55, "waimarama": 0.40,
    "wainui-beach": 1.30, "whareakeake-murdering-bay": 0.45,
}


def build_slot_keys_10d():
    today = nz_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return [slot_key((today + timedelta(days=d)).replace(hour=h))
            for d in range(DAYS) for h in (0, 6, 12, 18)]


def fetch_metocean(lat, lng, from_utc, n_slots):
    body = json.dumps({"points": [{"lon": lng, "lat": lat}], "variables": MO_VARS,
                       "time": {"from": from_utc, "interval": "6h",
                                "repeat": n_slots - 1}}).encode()
    import urllib.request
    req = urllib.request.Request(MO_URL, data=body, headers={
        "Content-Type": "application/json", "x-api-key": METOCEAN_KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    out = {}
    for v in MO_VARS:
        var = d["variables"].get(v, {})
        data, nod = var.get("data"), var.get("noData")
        out[v] = [x if (nod is None or nod[i] == 0) else None
                  for i, x in enumerate(data or [])]
    return out


def om_batch(base_url, coords, hourly, chunk=12, extra=""):
    """10-day Open-Meteo batch, NZ timezone (same slot_key convention as main)."""
    res = []
    for i in range(0, len(coords), chunk):
        ch = coords[i:i + chunk]
        lats = ",".join(str(a) for a, _ in ch)
        lngs = ",".join(str(b) for _, b in ch)
        data = http_get(f"{base_url}?latitude={lats}&longitude={lngs}"
                        f"&hourly={hourly}{extra}&timezone=Pacific%2FAuckland"
                        f"&forecast_days={DAYS}&apikey={OM_KEY}", timeout=90)
        res.extend(data if isinstance(data, list) else [data])
    return res


def main():
    spots = sb_select("spots")
    print(f"{len(spots)} spots")
    slot_keys = build_slot_keys_10d()
    today_nz = nz_now().replace(hour=0, minute=0, second=0, microsecond=0)
    from_utc = (today_nz - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")

    coords = [(s["lineup_lat"], s["lineup_lng"]) for s in spots]
    print("Open-Meteo batches (10-day)…", flush=True)
    om_fc = om_batch("https://customer-api.open-meteo.com/v1/forecast", coords,
                     "wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code,"
                     "uv_index,temperature_2m,precipitation_probability",
                     extra="&wind_speed_unit=kn")
    om_mar = om_batch("https://customer-marine-api.open-meteo.com/v1/marine", coords,
                      "sea_surface_temperature")

    fetched_at = datetime.now(NZ_TZ).isoformat()
    all_rows, mo_fail = [], 0
    for idx, s in enumerate(spots):
        factor = M1_FACTORS.get(s["id"], 1.0)
        try:
            mo = fetch_metocean(s["lineup_lat"], s["lineup_lng"], from_utc, len(slot_keys))
        except Exception as e:
            print(f"  MO FAIL {s['id']}: {str(e)[:100]}", flush=True)
            mo_fail += 1
            continue
        fc = om_fc[idx]["hourly"] if idx < len(om_fc) else None
        mar = om_mar[idx]["hourly"] if idx < len(om_mar) else None
        fc_idx = {slot_key(datetime.fromisoformat(t)): i
                  for i, t in enumerate(fc["time"])} if fc else {}
        mar_idx = {slot_key(datetime.fromisoformat(t)): i
                   for i, t in enumerate(mar["time"])} if mar else {}

        for j, key in enumerate(slot_keys):
            tot = mo["wave.height"][j] if j < len(mo["wave.height"]) else None
            if tot is None:
                continue
            a_h = mo["wave.height.above-8s"][j]
            b_h = mo["wave.height.below-8s"][j]
            a_p = mo["wave.period.above-8s.peak"][j]
            b_p = mo["wave.period.below-8s.peak"][j]
            a_d = mo["wave.direction.above-8s.peak"][j]
            b_d = mo["wave.direction.below-8s.peak"][j]
            wave_m = round(tot * factor, 2)
            # dominant band = the bigger one; its peak is THE period (GSN convention)
            if (a_h or 0) >= (b_h or 0):
                prim_h, prim_p, prim_d, sec_h, sec_p, sec_d = a_h, a_p, a_d, b_h, b_p, b_d
            else:
                prim_h, prim_p, prim_d, sec_h, sec_p, sec_d = b_h, b_p, b_d, a_h, a_p, a_d
            pk = mo["wave.period.peak"][j]
            period_s = round(pk, 1) if pk is not None else (
                round(prim_p, 1) if prim_p is not None else None)
            swell_deg = round(prim_d) if prim_d is not None else None
            prim_swell_h = round(prim_h * factor, 2) if prim_h is not None else None
            sec_swell_h = round(sec_h * factor, 2) if sec_h is not None else None
            sec_swell_period = round(sec_p, 1) if sec_p is not None else None
            sec_swell_deg = round(sec_d) if sec_d is not None else None

            fi, mi = fc_idx.get(key), mar_idx.get(key)
            wind_kt = fc["wind_speed_10m"][fi] if fi is not None else None
            wind_deg = fc["wind_direction_10m"][fi] if fi is not None else None
            wind_gust = fc["wind_gusts_10m"][fi] if fi is not None else None
            wcode = fc["weather_code"][fi] if fi is not None else None
            uv = fc["uv_index"][fi] if fi is not None else None
            air_c = fc["temperature_2m"][fi] if fi is not None else None
            precip = fc["precipitation_probability"][fi] if fi is not None else None
            water_c = mar["sea_surface_temperature"][mi] if mi is not None else None

            date_part, hh = key.split("T")
            slot_dt = datetime.strptime(date_part, "%Y-%m-%d").replace(
                hour=int(hh), tzinfo=NZ_TZ)

            rating = compute_rating(
                {"wave_m": wave_m, "period_s": period_s, "swell_deg": swell_deg,
                 "wind_kt": wind_kt, "wind_deg": wind_deg,
                 "prim_swell_h": prim_swell_h, "sec_swell_h": sec_swell_h}, s)

            all_rows.append({
                "spot_id": s["id"], "slot_key": key,
                "slot_time": slot_dt.isoformat(),
                "day_offset": (slot_dt.date() - today_nz.date()).days,
                "wave_m": wave_m, "wind_kt": wind_kt, "wind_deg": wind_deg,
                "wind_gust": wind_gust, "period_s": period_s, "swell_deg": swell_deg,
                "prim_swell_h": prim_swell_h, "sec_swell_h": sec_swell_h,
                "sec_swell_period": sec_swell_period, "sec_swell_deg": sec_swell_deg,
                "windwave_h": None,
                "weather_code": wcode, "air_c": air_c, "water_c": water_c,
                "precip_pct": precip, "uv": uv,
                "fetched_at": fetched_at,
                **rating,
            })
        if (idx + 1) % 20 == 0:
            print(f"  {idx+1}/{len(spots)}", flush=True)
        time.sleep(0.1)

    if mo_fail > len(spots) // 4:
        raise RuntimeError(f"MetOcean mostly down: {mo_fail} spots failed, not writing")
    print(f"\nwriting {len(all_rows)} rows to {STAGING_TABLE}…", flush=True)
    CHUNK = 200
    for i in range(0, len(all_rows), CHUNK):
        sb_upsert_chunk(STAGING_TABLE, all_rows[i:i + CHUNK],
                        on_conflict="spot_id,slot_key")
    print("done")


if __name__ == "__main__":
    main()
