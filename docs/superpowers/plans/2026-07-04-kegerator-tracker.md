# Kegerator Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and publish a live kegerator price tracker with PS5/TV dashboard interlinking.

**Architecture:** Static GitHub Pages dashboard fetches `data/listings.json`, `data/specs.json`, and `history.csv` at page load. Python refresh code normalizes specs, attempts polite cached fetches, rewrites current listings, and appends history rows without rewriting old rows. PS5/TV gets a shared tracker navigation strip.

**Tech Stack:** Python 3, pytest, vanilla HTML/CSS/JS, canvas charts, GitHub Actions, GitHub Pages.

## Global Constraints

- Keep dashboards granular; do not merge PS5/TV and kegerator data.
- Use the Raptor/PS5 visual family.
- Never fabricate confirmed prices.
- Respect robots.txt, cache responses, rate-limit at least 3 seconds, and use a real User-Agent.
- History is append-only.
- Scheduled commit message is exactly `Refresh Kegerator dashboard data`.
- Email payload recipients are exactly `lukestambaugh75@gmail.com` and `devin.mullen89@gmail.com`, no CC/BCC.

---

### Task 1: Repository Skeleton And Seed Data

**Files:**
- Create: `README.md`, `OPEN-ME-FIRST.md`, `Makefile`, `.gitignore`, `requirements.txt`
- Create: `data/specs.json`, `data/listings.json`, `history.csv`
- Create: `.github/workflows/refresh.yml`

**Interfaces:**
- Produces the canonical data files consumed by refresh, tests, and dashboard.

- [ ] Add files and seed data.
- [ ] Run JSON validation.
- [ ] Commit the skeleton.

### Task 2: Refresh, History, And Email Payload

**Files:**
- Create: `scripts/refresh.py`
- Create: `scripts/check_public_pages.py`
- Create: `scripts/serve_dashboard.py`
- Create: `tools/build_email.py`

**Interfaces:**
- `compute_garage_suitability(spec: dict) -> str`
- `normalize_listing(listing: dict, specs_by_model: dict, retrieved: str) -> dict`
- `append_history(listings: list[dict], path: Path, today: str) -> int`
- `build_payload(listings: list[dict], specs: list[dict], dashboard_url: str) -> dict`

- [ ] Write failing tests for suitability, discounts, history dedupe, and recipients.
- [ ] Implement the functions and scripts.
- [ ] Run pytest and `make verify`.
- [ ] Commit refresh and payload tooling.

### Task 3: Interactive Dashboard

**Files:**
- Create: `index.html`
- Add: `assets/kegerator-hero.png`

**Interfaces:**
- Fetches `data/listings.json`, `data/specs.json`, and `history.csv`.
- Renders KPIs, filters, sortable listings, garage-ready picks, spreads, movers, and history chart.

- [ ] Add dashboard tests for required sections and fetch behavior.
- [ ] Implement responsive static dashboard.
- [ ] Verify locally with tests and browser-readable HTML.
- [ ] Commit dashboard.

### Task 4: PS5/TV Interlink

**Files:**
- Modify: `/Users/lukestambaugh/Documents/Files for GitHub/PS5 and TV Deal Tracker r0/tools/render_dashboard.py`
- Modify: `/Users/lukestambaugh/Documents/Files for GitHub/PS5 and TV Deal Tracker r0/tests/test_tracker.py`
- Regenerate: `/Users/lukestambaugh/Documents/Files for GitHub/PS5 and TV Deal Tracker r0/index.html`

**Interfaces:**
- Adds the same compact tracker nav with links to PS5/TV, Kegerator, and Ford Raptor dashboards.

- [ ] Run `git pull --ff-only` before editing PS5/TV.
- [ ] Add failing test for the kegerator link.
- [ ] Update renderer and regenerate dashboard.
- [ ] Run PS5/TV `make verify`.
- [ ] Commit and push only touched PS5/TV files.

### Task 5: Publish And Connector Follow-Up

**Files:**
- No code files unless connector setup requires user-mediated steps.

**Interfaces:**
- New GitHub repo `lukestambaugh75-hue/kegerator-tracker-r0`.
- Public Pages URL `https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/`.

- [ ] Create GitHub repo and push `main`.
- [ ] Enable GitHub Pages from root on `main`.
- [ ] Run public page check when Pages is available.
- [ ] Try Gmail connector repair after dashboard work.
- [ ] If OAuth needs UI approval, stop and report the exact required action.
