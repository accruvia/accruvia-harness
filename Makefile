PYTHON ?= .venv/bin/python
PIP ?= .venv/bin/pip
PYTHONPATH_VALUE := src

.PHONY: help venv init install bootstrap install-temporal install-observability run ui-restart test test-fast test-e2e test-observer test-temporal temporal-up temporal-down

help:
	@echo "Targets:"
	@echo "  ./bin/accruvia-harness ... Run the self-bootstrapping local CLI"
	@echo "  make venv                  Create .venv"
	@echo "  make init                  Install editable package"
	@echo "  make bootstrap             Bootstrap .venv and install editable package"
	@echo "  make install-temporal      Install Temporal extras"
	@echo "  make install-observability Install observability extras"
	@echo "  make run ARGS=\"status\"     Run the harness CLI"
	@echo "  make ui-restart             Restart the Harness UI deterministically"
	@echo "  make test-fast             Run the fast suite"
	@echo "  make test                  Run the full suite"
	@echo "  make test-e2e              Run end-to-end tests"
	@echo "  make test-temporal         Run the Temporal runtime gate"
	@echo "  make test-observer         Run observer tests"
	@echo "  make temporal-up           Start Temporal dev stack"
	@echo "  make temporal-down         Stop Temporal dev stack"

venv:
	python3 -m venv .venv

init: venv
	$(PIP) install -e .
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) -m accruvia_harness init-db >/dev/null

install: init

bootstrap: init

install-temporal: venv
	$(PIP) install -e '.[temporal]'

install-observability: venv
	$(PIP) install -e '.[observability]'

run:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) -m accruvia_harness $(ARGS)

ui-restart:
	./bin/restart-ui

test-fast:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) -m unittest \
		tests.test_engine \
		tests.test_store \
		tests.test_validation \
		tests.test_workers \
		tests.test_phase1 \
		tests.test_interrogation \
		tests.test_observer \
		tests.test_parallel -v

test-e2e:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) -m unittest \
		tests.test_cli \
		tests.test_llm_e2e \
		tests.test_temporal_e2e -v

test-temporal: install-temporal temporal-up
	@trap 'docker compose -f docker-compose.temporal.yml down' EXIT; \
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) -m unittest \
		tests.test_runtime \
		tests.test_temporal_e2e -v

test-observer:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) -m unittest tests.test_observer -v

test:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) -m unittest discover -s tests -v

temporal-up:
	docker compose -f docker-compose.temporal.yml up -d

temporal-down:
	docker compose -f docker-compose.temporal.yml down
