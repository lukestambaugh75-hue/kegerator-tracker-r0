# Kegerator Tracker Design

## Goal

Build a public, live kegerator price tracker that matches Luke's Raptor and PS5/TV tracker family while making Houston garage heat suitability the main buying dimension.

## Scope

- Track Kegco, EdgeStar, Danby, Summit, and VEVOR kegerator rows from Home Depot, Kegco.com, EdgeStar.com, Danby via Home Depot, Summit via Home Depot, and optional Amazon/Keepa later.
- Keep the tracker granular: its own repo, data files, history ledger, workflow, and dashboard.
- Interlink the kegerator and PS5/TV dashboards with a compact tracker navigation strip, without merging their data or pages.
- Use public GitHub Pages. Luke is not worried about these trackers being public.
- Prepare email payloads for exactly `lukestambaugh75@gmail.com` and `devin.mullen89@gmail.com`, no CC/BCC, but do not send to Devin without explicit send confirmation.

## Existing Pattern To Match

The tracker should follow the same operating pattern as the Raptor and PS5/TV trackers:

- Static GitHub Pages dashboard.
- Durable JSON current snapshot.
- Append-only `history.csv`.
- Python refresh and verification scripts.
- Dark operational visual style with first-screen decision cards.
- Visible caveats for blocked, estimated, stale, or lower-confidence data.
- Commit message convention for scheduled data refreshes: `Refresh Kegerator dashboard data`.

## Data Model

`data/listings.json` is an array of current retailer observations:

`brand`, `model`, `description`, `tap_count`, `finish`, `type`, `complete_kit`, `retailer`, `current_price`, `list_price`, `discount_pct`, `in_stock`, `garage_suitability`, `outdoor_rated`, `source_url`, `data_quality`, and `retrieved`.

`data/specs.json` is an array of model reference rows:

`brand`, `model`, `tap_count`, `finish`, `type`, `complete_kit`, `temp_low_f`, `temp_high_f`, `deep_chill`, `fan_forced`, `outdoor_rated`, `garage_suitability`, `keg_capacity`, `dims_hwd_in`, `digital_control`, and `notes`.

`history.csv` remains append-only with:

`date,brand,model,retailer,price,list_price,source,data_quality`

Refresh code may append new rows, but must not rewrite prior rows.

## Garage Suitability Logic

The dashboard promotes this as a visible badge and filter:

- `Best - outdoor rated` when `outdoor_rated` is true.
- `Good - deep-chill + fan-forced` when `deep_chill` and `fan_forced` are true.
- `Good - low-30s headroom` when `temp_low_f <= 32`.
- `Fair - limited cold headroom` when `temp_low_f >= 35`.

This is the key differentiator. A closed Houston garage can reach 100-115F, while ordinary freestanding indoor units are often rated around 78-80F ambient. Outdoor rating and cold-headroom are treated as buying signals, not trivia.

## Dashboard Reading Path

First screen:

- Hero image using a real bitmap kegerator-in-garage asset.
- Cross-tracker nav.
- Lowest complete single tap.
- Lowest complete dual tap.
- Lowest outdoor-rated row.
- Overall price range.
- Number of models tracked.
- Last refresh timestamp.

Main body:

- Garage-ready section, sorted outdoor-rated first and then deep-chill/fan-forced.
- Filterable/sortable listings table.
- Cross-retailer spread by model: min, max, spread, best retailer.
- Discount and price-drop highlights.
- Per-model history chart with 30/90/all controls.
- Source and refresh caveats.

Mobile path:

- KPIs stack first.
- Filters remain compact.
- Listing rows become readable card-like table rows without horizontal scrolling.
- Essential values remain visible without hover.

## Visual Direction

Use the Raptor dashboard's density and confidence as the high bar, with the PS5/TV tracker brought into the same family through shared navigation. The kegerator page should feel work-focused and decision-oriented, not like a marketing landing page.

Color roles:

- Cold blue for cooling/headroom.
- Green for buy-ready/outdoor-rated.
- Amber for garage-heat caution or estimated data.
- Red only for blocked, stale, or out-of-stock warnings.

Cards remain at 8px radius. Avoid decorative gradients, blobs, one-note palettes, and hover-only meaning.

## Refresh Rules

- Respect robots.txt with `urllib.robotparser`.
- Use a real User-Agent.
- Cache source responses under `.cache/http/`.
- Rate-limit live source requests by at least 3 seconds.
- Prefer structured JSON-LD/product metadata or conservative price regexes.
- If a source blocks or no price can be parsed, keep the existing row as `estimated` or skip append logic where appropriate; never invent a confirmed price.
- Deduplicate history appends by `(date, brand, model, retailer)`.

## Testing

Use pytest with no live network in tests. Cover:

- Garage suitability calculation.
- Discount calculation.
- History append dedupe.
- Dashboard fetches JSON and CSV rather than embedding stale data.
- Email payload recipients are exactly Luke and Devin, no CC/BCC.
- Seed data includes the requested models and July 4 confirmed rows.

## Publishing

GitHub Actions runs daily at 11:00 UTC and also supports manual `workflow_dispatch`. It runs `scripts/refresh.py`, commits changed data/history with `Refresh Kegerator dashboard data`, and pushes to `main`.

GitHub Pages serves `index.html` from `main` root.
