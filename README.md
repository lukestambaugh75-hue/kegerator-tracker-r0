# Kegerator Tracker

Public live dashboard for kegerator prices, with Houston garage heat suitability treated as a first-class buying signal.

Dashboard URL:

https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/

## What It Tracks

- Kegco, EdgeStar, Danby, Summit, and VEVOR kegerators.
- Home Depot, Kegco.com, EdgeStar.com, Danby via Home Depot, Summit via Home Depot.
- Amazon/Keepa can be added later with `KEEPA_API_KEY`; no Amazon prices are fabricated without that evidence.

## Files

- `data/listings.json` - current model x retailer price observations.
- `data/specs.json` - reference specs including cooling range, fan-forced cooling, outdoor rating, and computed garage suitability.
- `history.csv` - append-only ledger with `date,brand,model,retailer,price,list_price,source,data_quality`.
- `scripts/refresh.py` - normalizes data, attempts polite cached source checks, rewrites listings/specs, and appends new history rows.
- `.github/workflows/refresh.yml` - daily 11:00 UTC refresh with manual dispatch.
- `tools/build_email.py` - creates a reviewable email payload for Luke and Devin only.
- `automation/kegerator-tracker-email.toml` - repo mirror of the Codex email automation run contract.

## Local Commands

```bash
make refresh
make verify
make open
```

`make verify` runs the refresh, JSON validation, pytest, email payload generation, and whitespace checks.

## Data Quality

`confirmed` means a row came from a confirmed source snapshot or live parse. `snapshot_varies` means the same source may show different visible placements in the same day. `estimated` means the refresh could not confirm a new price and preserved the last known value as an estimate instead of pretending it is freshly confirmed.

No row should be promoted as confirmed unless the source supplied the price. If a source blocks, the dashboard keeps the caveat visible.

## Adding Models

Add a spec row to `data/specs.json`. Add one or more retailer observation rows to `data/listings.json` only when there is a traceable source URL and price evidence. The dashboard and refresh code consume these config/data files without code changes.

## GitHub Pages

Pages should serve from the `main` branch root. `index.html` fetches `data/listings.json`, `data/specs.json`, and `history.csv` at load, so the dashboard reflects the latest committed data without a rebuild.

## Email

Generated email payloads are addressed exactly to:

- `lukestambaugh75@gmail.com`
- `devin.mullen89@gmail.com`

No CC/BCC. This repo generates `out/latest-email.json`; sending uses the approved signed-in Chrome/Gmail browser route so it does not depend on the Gmail connector OAuth scope. Before sending, verify the two recipient chips, no CC/BCC, subject, body, and dashboard link.

The automation mirror lives at `automation/kegerator-tracker-email.toml`. It is marked `READY_TO_REGISTER` because Codex.app scheduled jobs are registered in the app UI, not from this repo.
