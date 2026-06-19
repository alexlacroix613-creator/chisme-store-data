# chisme-store-data

Auto-refreshed Tequila Chisme Alberta store-locator data, served to chisme.party.

`chisme-alberta-stores.json` is regenerated weekly from the public LiquorConnect
OData feed and published via GitHub Pages with permissive CORS so the website can
fetch it live from the browser.

**Public URL (CORS-enabled):**
`https://alexlacroix613-creator.github.io/chisme-store-data/chisme-alberta-stores.json`

## Auto-refresh (Optimus)

A weekly job on **Optimus** (the always-on Mac Mini M4) regenerates the data and
pushes it here. The website then reads the fresh JSON within ~1-2 min of the push
(GitHub Pages rebuild).

- **Generator:** `scripts/scrape_chisme_ab.py` — pulls the public LiquorConnect /
  AGLC OData feed (no auth) for SKU 143270 (Tequila Chisme Blanco), geocodes any
  missing pins via Nominatim, and writes `chisme-alberta-stores.json`.
- **Runner:** `scripts/refresh.sh` — generate → sanity-check → commit → push →
  append to `run.log`.
- **Schedule:** launchd `com.siempre.chisme-locator-refresh`, Mondays 06:00 MT.
- **Clone on Optimus:** `~/chisme-store-data`.

### Dead-man's switch

`run.log` gets one line per run:

```
run_ok   2026-06-19T13:00:11Z 48
run_fail 2026-06-26T13:00:09Z generator_error
```

This is a **weekly** job, so the rule is: **if the latest `run_ok` in `run.log`
is more than 8 days old, the job has died** — investigate `launchd.err` /
`launchd.out` in the clone, then re-run `scripts/refresh.sh` by hand. (Same
pattern as the VENN forwarder's "no fresh run_ok in >26h = died", scaled from
hourly to weekly.)

Quick check on Optimus:

```bash
tail -n 5 ~/chisme-store-data/run.log
# last run_ok older than 8 days => DEAD, investigate.
```
