UV ?= uv
AS_OF ?=

.PHONY: install universe ingest screen research report test lint

install:
	$(UV) sync

universe:
	$(UV) run pea universe

ingest:
	$(UV) run pea ingest

screen:
	$(UV) run pea screen $(if $(AS_OF),--as-of $(AS_OF),)

research:
	$(UV) run pea research

report:
	$(UV) run pea report $(if $(AS_OF),--as-of $(AS_OF),)

test:
	$(UV) run pytest -q

lint:
	$(UV) run ruff check pea tests
