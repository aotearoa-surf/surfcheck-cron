"""Auto-apply calibration approvals from the dashboard into the live shelter file.

Runs hourly (apply_approvals.yml). Makes the LIVE bin factors in
_shelter_curves.json (+ the spot_factors mirror) match the decisions a human made
on the calibration dashboard, so an "Approve" click is the whole action - no
Claude step. Reversible: "Remove" on the dashboard pulls a factor back out.

Per-spot state is driven by the newest row in calibration_proposals whose status
is one of {approved, applied, removed} (the LIVE-state decisions):
  approved -> apply proposed_factors, then mark the row 'applied'
  applied  -> already live, keep as-is (idempotent)
  removed  -> pull the factor out of the file + spot_factors (un-approve)
Rows with status pending / needs_data / rejected are IGNORED here:
  - pending/needs_data are undecided
  - rejected just declines a *suggestion*; it must never strip a spot's existing
    live factor (that's what 'removed' is for).
Spots with no such decision row (e.g. the original hand-fitted factors) are left
untouched.

Only BIN factors flow through the dashboard; smooth (D2) and flat curves stay in
code and are not touched here.
"""
import os, json, sys, time, urllib.request, urllib.error

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
H = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
FILE = "_shelter_curves.json"
LIVE = ("approved", "applied", "removed")


# Transient network/gateway blips (e.g. Supabase 504 Gateway Timeout, or a read
# timeout) must not fail the whole run + email on a red job. Retry those a few
# times with a short backoff; only a persistent failure raises.
_TRANSIENT_CODES = {500, 502, 503, 504}


def _open(req, tries=4, backoff=2):
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code in _TRANSIENT_CODES and i < tries - 1:
                print(f"  transient HTTP {e.code} on {req.get_method()} {req.full_url} "
                      f"- retry {i + 1}/{tries - 1}", flush=True)
                time.sleep(backoff * (i + 1))
                continue
            raise
        except urllib.error.URLError as e:  # timeouts, connection resets, DNS
            if i < tries - 1:
                print(f"  transient {type(e).__name__} ({e.reason}) on {req.get_method()} "
                      f"{req.full_url} - retry {i + 1}/{tries - 1}", flush=True)
                time.sleep(backoff * (i + 1))
                continue
            raise


def sb_get(path):
    r = urllib.request.Request(f"{URL}/rest/v1/{path}", headers=H)
    _, body = _open(r)
    return json.loads(body)


def sb_write(path, body, method, prefer="return=minimal"):
    r = urllib.request.Request(f"{URL}/rest/v1/{path}", data=json.dumps(body).encode() if body is not None else None,
                               headers={**H, "Prefer": prefer}, method=method)
    status, _ = _open(r)
    return status


def sb_delete(path):
    r = urllib.request.Request(f"{URL}/rest/v1/{path}", headers=H, method="DELETE")
    status, _ = _open(r)
    return status


def main():
    rows = sb_get("calibration_proposals?select=id,spot_id,status,proposed_factors,created_at"
                  "&order=created_at.desc")
    # newest LIVE-state decision per spot (rows already newest-first)
    ctrl = {}
    for r in rows:
        if r["status"] in LIVE and r["spot_id"] not in ctrl:
            ctrl[r["spot_id"]] = r

    d = json.load(open(FILE, encoding="utf-8"))
    applied_spots = set(d["_meta"]["applied_spots"])
    changes = []

    for spot, r in ctrl.items():
        st = r["status"]
        if st in ("approved", "applied"):
            factors = r.get("proposed_factors") or {}
            if not factors:
                continue
            if d.get(spot) != factors:
                d[spot] = factors
                applied_spots.add(spot)
                changes.append(f"apply {spot}={json.dumps(factors)}")
                sb_write("spot_factors?on_conflict=spot_id",
                         {"spot_id": spot, "model": "bin", "factors": factors},
                         "POST", prefer="resolution=merge-duplicates,return=minimal")
            if st == "approved":
                sb_write(f"calibration_proposals?id=eq.{r['id']}", {"status": "applied"}, "PATCH")
        elif st == "removed":
            if spot in d:
                del d[spot]
                applied_spots.discard(spot)
                changes.append(f"remove {spot}")
                try:
                    sb_delete(f"spot_factors?spot_id=eq.{spot}")
                except urllib.error.HTTPError:
                    pass

    changed = bool(changes)
    if changed:
        d["_meta"]["applied_spots"] = sorted(applied_spots)
        with open(FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
            f.write("\n")
    print("\n".join(changes) if changes else "no approvals to apply")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"changed={'1' if changed else '0'}\n")


if __name__ == "__main__":
    main()
