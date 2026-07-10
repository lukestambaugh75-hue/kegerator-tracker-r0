PY := /usr/bin/python3
PORT ?= 8767

.PHONY: refresh json test audience verify email-content open serve pages-check

refresh:
	$(PY) scripts/refresh.py

json:
	$(PY) -m json.tool data/listings.json >/dev/null
	$(PY) -m json.tool data/specs.json >/dev/null

test:
	$(PY) -m pytest -q

audience: email-content
	$(PY) scripts/audience_guard.py

verify: refresh json test audience
	git diff --check

email-content:
	$(PY) tools/build_email.py --output-dir out

open:
	$(PY) scripts/serve_dashboard.py --port $(PORT)

serve:
	$(PY) scripts/serve_dashboard.py --port $(PORT) --no-browser

pages-check:
	$(PY) scripts/check_public_pages.py
