# SurfCheck cron

Scheduled data jobs for [SurfCheck.nz](https://surfcheck.nz). They run on GitHub
Actions and write forecast + tide data into Supabase. No website code here, just
the two fetch scripts.

## Jobs

| Workflow | Schedule | Script | What it does |
|---|---|---|---|
| Forecast fetch | every 3 hours | `_fetch_main.py` | Stormglass + Open-Meteo per spot, computes ratings, refreshes `spot_now` |
| Tide fetch | daily (~1am NZ) | `_fetch_tides.py` | Refreshes NIWA tide columns on existing rows |

Both read the spot + pin lists straight from Supabase, so there is nothing to
keep in sync here.

## Required GitHub Secrets

Set under **Settings → Secrets and variables → Actions**:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `STORMGLASS_KEY`   (forecast job only)
- `NIWA_KEY`         (tide job only)

## Run manually

**Actions** tab → pick a workflow → **Run workflow**. A healthy forecast run
takes about 5 minutes. Each job has a timeout, so a hang self-kills instead of
freezing future runs.

## Run locally (optional backup)

Needs a local `.env` with the four keys above, then:

```
pip install -r requirements.txt
python -u _fetch_main.py
python -u _fetch_tides.py
```
