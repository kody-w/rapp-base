PYTHON ?= python3
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: build test test-python check check-python check-node check-pages

build:
	$(PYTHON) scripts/build.py

test-python:
	$(PYTHON) -m unittest discover -s tests -v

check-node:
	@if command -v node >/dev/null 2>&1; then \
		node --check sdk/rapp-base.js && \
		node --input-type=module --check < explorer.js && \
		node --test sdk/rapp-base.test.mjs; \
	elif [ "$${REQUIRE_NODE:-0}" = "1" ]; then \
		echo "Node is required but not available" >&2; exit 1; \
	else \
		echo "Node not available; skipped SDK tests"; \
	fi

test: test-python check-node

check-python:
	$(PYTHON) scripts/build.py --check
	$(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) scripts/check.py

check-pages:
	$(PYTHON) scripts/prepare_pages.py --output .pages
	rm -rf .pages

check: check-python check-node check-pages
