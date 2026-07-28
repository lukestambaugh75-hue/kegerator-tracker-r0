# Kegerator Tracker

Start here for local work:

```bash
make verify
make open
```

Public dashboard after Pages is enabled:

https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/

This page is intentionally direct-link-only for Luke + Devin. It must not link to, or load runtime assets from, any other dashboard repository.

Core files:

- `data/listings.json` - current retailer observations.
- `data/specs.json` - model specs and garage suitability inputs.
- `history.csv` - append-only price observations.
- `scripts/refresh.py` - polite cached refresh plus history append.
- `scripts/audience_guard.py` - standalone link, runtime asset, and recipient boundary check.
- `scripts/run_evidence.py` - executes and records the fixed per-run refresh, verification, bounded repair, and deployment checks; it cannot commit, push, send, or claim delivery.
- `scripts/check_public_pages.py` - exact result-Git-blob/live-origin/public-response verification.
- `index.html` - GitHub Pages dashboard that fetches JSON/CSV at load.

Run `make audience` for the local boundary check and `make pages-check` for the deployed-page check. After a refresh has already run once, use `make verify-current` so verification cannot trigger a second acquisition attempt. The scheduled lane uses `scripts/run_evidence.py`; do not run the underlying refresh separately there.
