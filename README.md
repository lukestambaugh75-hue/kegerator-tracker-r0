# Kegerator Tracker

Public live dashboard for kegerator prices, with Houston garage heat suitability treated as a first-class buying signal.

This is a direct-link-only Luke + Devin dashboard. It intentionally has no shared-dashboard, PS5/TV, Ford/Raptor, or other cross-repository navigation or runtime assets.

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
- `scripts/audience_guard.py` - fail-closed check for the standalone link graph, exact current listing sources, local runtime files, and Luke + Devin recipient boundary.
- `scripts/run_evidence.py` - detached per-run terminal summary; it binds refresh, result, deployment, payload, and outer receipt evidence without performing any of those actions.
- `scripts/check_public_pages.py` - proves the public files are byte-for-byte the clean pushed local `HEAD`, not merely internally consistent public files.
- `.github/workflows/refresh.yml` - daily 11:00 UTC refresh with manual dispatch.
- `tools/build_email.py` - creates a reviewable email payload for Luke and Devin only.
- `automation/kegerator-tracker-email.toml` - repo mirror of the Codex email automation run contract.

## Local Commands

```bash
make refresh
make verify
make verify-current
make audience
make pages-check
make open
```

`make verify` runs one refresh followed by the complete local verification. Automation that already ran its one refresh uses `make verify-current`, which cannot trigger a second acquisition attempt. `make audience` is the local boundary check. `make pages-check` now requires the deployed HTML, image, JSON, and CSV bytes to match the clean local `HEAD` and live `origin/main` identity in addition to applying the public boundary checks.

The audience guard pins the exact `index.html` path and SHA-256 bytes before applying its parser checks. Any intentional dashboard HTML, CSS, or inline JavaScript edit must update the pinned digest and tests in the same reviewed commit; GitHub Pages must serve those exact bytes.

## Data Quality

`confirmed` means a row came from a confirmed source snapshot or live parse. `snapshot_varies` means the same source may show different visible placements in the same day. `estimated` means the refresh could not confirm a new price and preserved the last known value as an estimate instead of pretending it is freshly confirmed.

No row should be promoted as confirmed unless the source supplied the price. If a source blocks, the dashboard keeps the caveat visible.

## Adding Models

Add a spec row to `data/specs.json`. Add one or more retailer observation rows to `data/listings.json` only when there is a traceable source URL and price evidence. The dashboard and refresh code consume these config/data files without code changes.

## GitHub Pages

Pages should serve from the `main` branch root. `index.html` fetches `data/listings.json`, `data/specs.json`, and `history.csv` at load, so the dashboard reflects the latest committed data without a rebuild. Share this page by its direct URL; do not place it in a shared dashboard navigation or load assets from another repository.

## Email

Generated email payloads are addressed exactly to:

- `lukestambaugh75@gmail.com`
- `devin.mullen89@gmail.com`

No CC/BCC. This repo generates `out/latest-email.json`; the builder refuses any alternate dashboard URL or recipient set and embeds an identity for the exact listings, specs, and refresh-status inputs. A payload built before input drift is rejected. Sending uses the approved signed-in Chrome/Gmail browser route so it does not depend on the Gmail connector OAuth scope. Before sending, verify the two recipient chips, no CC/BCC, subject, body, dashboard link, and a passing audience guard.

## Detached Run Evidence

The local email lane records canonical, ignored, nonsymlink evidence under `out/`; it must never be committed. `out/run-state.json` is a non-authoritative terminal summary with run, workflow, lane, owner-process, live-origin, start/finish UTC, start/result commit SHAs, recovery evidence, and ordered preflight, freshness, blocker, repair, verification, deployment, payload, and receipt stages. Each observed stage carries the run ID and refresh source SHA.

The adapter executes the fixed refresh, local verification, allowlisted history repair, and public-deployment verification commands whose results it records. It does not commit, push, deploy, open a browser, or send mail. The one refresh is bound to the actual pre-run target count and manifest. Deployment passes only when the recorder fetches every canonical public URL itself and those exact bytes match Git blobs from the clean pushed result SHA and approved live origin. Payload passes only when its schema, canonical serialization, deterministic content, generation window, and embedded source identity exactly match the current fresh run.

A repair is never inferred from a failure. The sole allowlisted repair is a one-attempt `history-prune` after its fixed check proves estimated history rows exist; its command, target path, tool/target hashes, exit code, and postcheck are recorded, and the full local verification must then rerun. Stale run recovery requires the exact old run ID, a dead recorded owner process, a 12-hour minimum age, and unchanged live origin identity. Early adapter failures close as `failed` rather than leaving a misleading running state.

This repository does not currently have a trusted external mail-receipt adapter. Arbitrary detached JSON, screenshots, and AI-authored observations are rejected as receipts. A browser send attempt can therefore finish only as `delivery_unverified`; the terminal summary never claims `delivered`.

The automation mirror lives at `automation/kegerator-tracker-email.toml`. Its status reflects the Codex.app scheduled job: `ACTIVE` after registration, or `READY_TO_REGISTER` before the app job exists.
