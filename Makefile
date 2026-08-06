PY := python3

.PHONY: help ingest ingest-all build serve test deploy

help:
	@echo "make ingest DATE=2026-07-15   aggregate one day into data/agg/"
	@echo "make ingest-all               aggregate every day present in R2 (resumable)"
	@echo "make build                    merge data/agg/ into web/data/*.json"
	@echo "make serve                    serve web/ on http://localhost:8000"
	@echo "make test                     run unit tests"
	@echo "make deploy                   build, then publish web/ as a Cloudflare Worker"
	@echo "make update                   ingest new days, rebuild, deploy"

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

update: ingest-all build
	npx wrangler deploy
