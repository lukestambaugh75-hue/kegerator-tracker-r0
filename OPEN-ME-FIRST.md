# Kegerator Tracker

Start here for local work:

```bash
make verify
make open
```

Public dashboard after Pages is enabled:

https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/

Core files:

- `data/listings.json` - current retailer observations.
- `data/specs.json` - model specs and garage suitability inputs.
- `history.csv` - append-only price observations.
- `scripts/refresh.py` - polite cached refresh plus history append.
- `index.html` - GitHub Pages dashboard that fetches JSON/CSV at load.
