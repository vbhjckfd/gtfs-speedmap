PY := python3

# So every target runs from a plain checkout, with or without `pip install -e .`.
export PYTHONPATH := src

.PHONY: help ingest ingest-all build serve test deploy pull push update

help:
	@echo "make ingest DATE=2026-07-15   aggregate one day into data/agg/"
	@echo "make ingest-all               aggregate every day present in R2 (resumable)"
	@echo "make build                    merge data/agg/ into web/data/*.json"
	@echo "make serve                    serve web/ on http://localhost:8000"
	@echo "make test                     run unit tests"
	@echo "make deploy                   build, then publish web/ as a Cloudflare Worker"
	@echo "make pull                     download aggregates from R2 (skips ones on disk)"
	@echo "make push                     upload new aggregates to R2"
	@echo "make update                   pull, ingest new days, push, rebuild, deploy"

ingest:
	$(PY) -m speedmap.aggregate $(DATE) $(ARGS)

ingest-all:
	$(PY) -m speedmap.aggregate --all $(ARGS)

build:
	$(PY) -m speedmap.build_web $(ARGS)

serve:
	cd web && $(PY) -m http.server 8000

test:
	$(PY) -m pytest -q

deploy: build
	npx wrangler deploy

pull:
	$(PY) -m speedmap.sync pull $(ARGS)

push:
	$(PY) -m speedmap.sync push $(ARGS)

update: pull ingest-all push build
	npx wrangler deploy
