.PHONY: test lint lint-imports compile typecheck gates

OFFLINE_SELECTOR := not live_provider and not live_antigravity and not real_host and not tokenizer_download

test:
	ARGUS_OFFLINE=1 pytest -q -m "$(OFFLINE_SELECTOR)"

lint:
	ruff check .

lint-imports:
	ruff check --select I .

compile:
	python -m compileall -q argus tests

typecheck:
	mypy argus

gates: lint compile test
