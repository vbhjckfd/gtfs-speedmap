PY := python3

# So every target runs from a plain checkout, with or without `pip install -e .`.
export PYTHONPATH := src

# Keeps a Mac awake through hours-long archive passes; empty where caffeinate
# does not exist, so the same targets still run on Linux.
CAFFEINATE := $(shell command -v caffeinate >/dev/null 2>&1 && echo caffeinate -i)

# Wrangler needs Node >= 22, pinned in .nvmrc. nvm is a shell function, so it is
# sourced in each recipe's own shell rather than inherited from yours; empty
# where nvm is not installed, leaving whatever node is on PATH.
NVM_SH := $(or $(NVM_DIR),$(HOME)/.nvm)/nvm.sh
NODE := $(shell test -s "$(NVM_SH)" && echo '. "$(NVM_SH)" && nvm use --silent &&')

.PHONY: help ingest build serve test deploy update pull push

help:
	@echo "make ingest                   read every finished day in R2 into data/ (resumable)"
	@echo "make ingest DATE=2026-07-15   just that day"
	@echo "    ARGS=--force              redo days already on disk"
	@echo "    ARGS='--force --only segments'   redo one pass: speed or segments"
	@echo "make build                    merge data/ into web/data/*.json"
	@echo "make serve                    serve web/ on http://localhost:8000"
	@echo "make test                     run unit tests"
	@echo "make deploy                   build, then publish web/ as a Cloudflare Worker"
	@echo "make update                   ingest new days, then deploy"
	@echo "make pull                     optional: download aggregates from R2"
	@echo "make push                     optional: back up aggregates to R2"

# One read of the archive feeds both products: the speed cells and the real
# stop-to-stop leg times the ruler's ride times are built from.
ingest:
	$(CAFFEINATE) $(PY) -m speedmap.ingest $(or $(DATE),--all) $(ARGS)

build:
	$(CAFFEINATE) $(PY) -m speedmap.build_web $(ARGS)

serve:
	cd web && $(PY) -m http.server 8000

test:
	$(PY) -m pytest -q

deploy: build
	$(NODE) npx wrangler deploy

# Sequenced in the recipe, not as prerequisites, so `make -j` cannot start the
# build before the new days are on disk; ARGS are the ingest's, not the build's.
update: ingest
	$(MAKE) deploy ARGS=

# Off the `update` path on purpose: a monthly run on the machine that already
# holds data/ has nothing to fetch, and a first push is a 330 MB upload that
# should be a decision, not a side effect.
pull:
	$(CAFFEINATE) $(PY) -m speedmap.sync pull $(ARGS)

push:
	$(CAFFEINATE) $(PY) -m speedmap.sync push $(ARGS)
