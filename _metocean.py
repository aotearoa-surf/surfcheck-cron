# -*- coding: utf-8 -*-
"""MetOcean marine source for the main cycle (M1, live 9 Aug 2026).

Fetch at the spot's LINEUP GPS, not an offshore pin: MetOcean's nearshore
model resolves coastal transformation, which is the whole reason for the swap
(measured 9 Aug: fleet MAE vs GoodSurfNow 0.078 m over 44 truth spots, vs
0.15-0.22 for the calibrated Stormglass system).

Variables: total height + peak period for display, the above/below-8s band
pairs for the two-tone bar and 2nd-swell row (GoodSurfNow's own convention),
band peak directions. wave.period.peak is the display period: measured 1.21 s
vs GSN against 1.82 s for a dominant-band-peak construction.

M1_FACTORS: per-spot multipliers fitted on the two 9 Aug GSN captures
(gsn_2026-08-09.json + gsn_2026-08-09_batch2.json, lead 0-2 days). Fits inside
0.90-1.10 are dropped to 1.0 so noise is not chased. Spots absent here run
factor 1.0. These are SEPARATE from spots.adjustment_factor on purpose: the
Stormglass factors stay untouched in the DB so MARINE_MODE=stormglass rolls
back cleanly.
"""
import json, os, urllib.request

MO_URL = "https://forecast-v2.metoceanapi.com/point/time"

MO_VARS = ["wave.height", "wave.period.peak",
           "wave.height.above-8s", "wave.period.above-8s.peak", "wave.direction.above-8s.peak",
           "wave.height.below-8s", "wave.period.below-8s.peak", "wave.direction.below-8s.peak"]

# Che, 9 Aug 2026 evening: "remove all adjustment factors for now and we
# reassess.. I dont want adjustment factors on any MetOcean spot data."
# MetOcean runs RAW at every spot. Reason: the fitted shadow factors were
# direction-blind and crushed real swell events (sweep found Sumner showing
# 0.33-0.58 m against GSN's 1.3-1.6 m Wed-Sat, Mahia north 0.54 vs 1.8).
# The fitted values are preserved below for the reassessment; do NOT
# reactivate without a full recommendation and Che's approval.
M1_FACTORS = {}

_FITTED_9AUG_FOR_REASSESSMENT = {
    "takapuna": 0.40, "omaha": 0.40, "okiwi-gbi": 0.45, "pauanui": 0.75,
    "sumner-scarborough": 0.45, "mahia-north-coast": 0.45,
    "hotwater-beach": 0.85, "waihi-beach": 0.85, "waipu-cove": 0.85, "matata": 1.15,
    "aramoana": 0.70, "cray-bay": 0.65, "gizzy-pipe": 1.20, "mahia-open-coast": 0.85,
    "meatworks": 1.20, "ocean-beach-hb": 0.75, "otama": 0.75,
    "raglan-tirohanga-indies": 0.85, "riverton": 0.65, "tauroa-shippies": 1.15,
    "te-awanga": 0.50, "the-cut-blenheim": 0.55, "waimarama": 0.40,
    "wainui-beach": 1.30, "whareakeake-murdering-bay": 0.45,
}


def fetch_metocean(lat, lng, from_utc, n_slots, timeout=60):
    """One spot, all 8 variables, n_slots six-hourly steps. Values whose
    noData code is non-GOOD come back as None."""
    body = json.dumps({"points": [{"lon": lng, "lat": lat}], "variables": MO_VARS,
                       "time": {"from": from_utc, "interval": "6h",
                                "repeat": n_slots - 1}}).encode()
    req = urllib.request.Request(MO_URL, data=body, headers={
        "Content-Type": "application/json",
        "x-api-key": os.environ["METOCEAN_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    out = {}
    for v in MO_VARS:
        var = d["variables"].get(v, {})
        data, nod = var.get("data"), var.get("noData")
        out[v] = [x if (nod is None or nod[i] == 0) else None
                  for i, x in enumerate(data or [])]
    return out


def metocean_slot_fields(mo, j, factor):
    """Map MetOcean arrays at slot index j onto our slot_forecast fields.
    Returns None when the slot has no total height (caller keeps stale row).
    prim = the BIGGER band (so the displayed swell row is the wave you see),
    period_s = overall spectrum peak (GSN's number)."""
    tot = mo["wave.height"][j] if j < len(mo["wave.height"]) else None
    if tot is None:
        return None
    a_h, b_h = mo["wave.height.above-8s"][j], mo["wave.height.below-8s"][j]
    a_p, b_p = mo["wave.period.above-8s.peak"][j], mo["wave.period.below-8s.peak"][j]
    a_d, b_d = mo["wave.direction.above-8s.peak"][j], mo["wave.direction.below-8s.peak"][j]
    if (a_h or 0) >= (b_h or 0):
        prim_h, prim_p, prim_d, sec_h, sec_p, sec_d = a_h, a_p, a_d, b_h, b_p, b_d
    else:
        prim_h, prim_p, prim_d, sec_h, sec_p, sec_d = b_h, b_p, b_d, a_h, a_p, a_d
    pk = mo["wave.period.peak"][j]
    return {
        "wave_m": round(tot * factor, 2),
        "period_s": round(pk, 1) if pk is not None else (
            round(prim_p, 1) if prim_p is not None else None),
        "swell_deg": round(prim_d) if prim_d is not None else None,
        "prim_swell_h": round(prim_h * factor, 2) if prim_h is not None else None,
        "sec_swell_h": round(sec_h * factor, 2) if sec_h is not None else None,
        "sec_swell_period": round(sec_p, 1) if sec_p is not None else None,
        "sec_swell_deg": round(sec_d) if sec_d is not None else None,
        "windwave_h": None,   # absorbed into the below-8s band, GSN convention
    }
