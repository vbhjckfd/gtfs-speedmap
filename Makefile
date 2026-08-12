PY := python3

# So every target runs from a plain checkout, with or without `pip install -e .`.
export PYTHONPATH := src

.PHONY: help ingest ingest-all segments segments-all build serve test deploy pull push update

help:
	@echo "make ingest DATE=2026-07-15   aggregate one day into data/agg/"
	@echo "make ingest-all               aggregate every day present in R2 (resumable)"
	@echo "make segments-all             time real stop-to-stop legs (resumable)"
	@echo "make build                    merge data/agg/ and data/seg/ into web/data/*.json"
	@echo "make serve                    serve web/ on http://localhost:8000"
	@echo "make test                     run unit tests"
	@echo "make deploy                   build, then publish web/ as a Cloudflare Worker"
	@echo "make update                   ingest new days, rebuild, deploy"
	@echo "make pull                     optional: download aggregates from R2"
	@echo "make push                     optional: back up aggregates to R2"

ingest:
	$(PY) -m speedmap.aggregate $(DATE) $(ARGS)

ingest-all:
	$(PY) -m speedmap.aggregate --all $(ARGS)

# The other pass over the same archive: real vehicles timed between stops, which
# is what the ruler's ride times are built from.
segments:
	$(PY) -m speedmap.segments $(DATE) $(ARGS)

segments-all:
	$(PY) -m speedmap.segments --all $(ARGS)

build:
	$(PY) -m speedmap.build_web $(ARGS)

serve:
	cd web && $(PY) -m http.server 8000

test:
	$(PY) -m pytest -q

deploy: build
	npx wrangler deploy

update: ingest-all segments-all build
	npx wrangler deploy

# Off the `update` path on purpose: a monthly run on the machine that already
# holds data/ has nothing to fetch, and a first push is a 330 MB upload that
# should be a decision, not a side effect.
pull:
	$(PY) -m speedmap.sync pull $(ARGS)

push:
	$(PY) -m speedmap.sync push $(ARGS)
