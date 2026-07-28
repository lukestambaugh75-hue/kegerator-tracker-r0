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

The local email lane records `out/run-state.json`, which is ignored and must never be committed. It is a non-authoritative terminal summary with run, workflow, and lane IDs; start/finish UTC; start/result commit SHAs; and ordered preflight, freshness, blocker, repair, deployment, payload, and receipt stages. Each observed stage carries the run ID and refresh source SHA.

The adapter does not refresh, repair, commit, push, deploy, open a browser, or send mail. It only validates and binds evidence from those outer steps. Deployment passes only when every dashboard runtime file in the explicit contract exactly matches the tracked local candidate, local `HEAD` equals live `origin/main`, and the bundle marker binds those bytes to the current refresh identity. Payload passes only when its embedded identity matches that same refresh. Receipt stays `unverified` until separate browser or mailbox evidence matches the exact payload SHA-256, run/workflow/lane IDs, recipients, subject, observed UTC time, and external receipt ID.

A repair is never inferred from a failure. One detached proposal can authorize one bounded, matching repair attempt; a second attempt is rejected. Ordinary tracker work requires no separate visual render or artifact.

The automation mirror lives at `automation/kegerator-tracker-email.toml`. Its status reflects the Codex.app scheduled job: `ACTIVE` after registration, or `READY_TO_REGISTER` before the app job exists.
