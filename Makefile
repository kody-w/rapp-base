PYTHON ?= python3
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: build test check

build:
	$(PYTHON) scripts/build.py

test:
	$(PYTHON) -m unittest discover -s tests -v
	@if command -v node >/dev/null 2>&1; then \
		node --check sdk/rapp-base.js && \
		node --input-type=module --check < explorer.js && \
		node --test sdk/rapp-base.test.mjs; \
	else \
		echo "Node not available; skipped SDK tests"; \
	fi

check:
	$(PYTHON) scripts/build.py --check
	$(PYTHON) -m unittest discover -s tests -v
	@if command -v node >/dev/null 2>&1; then \
		node --check sdk/rapp-base.js && \
		node --input-type=module --check < explorer.js && \
		node --test sdk/rapp-base.test.mjs; \
	else \
		echo "Node not available; skipped SDK tests"; \
	fi
	$(PYTHON) scripts/prepare_pages.py --output .pages
	rm -rf .pages
	$(PYTHON) scripts/check.py
