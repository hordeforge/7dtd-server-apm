ROOT := $(CURDIR)
# Resolve the dedicated-server default through the shared shell fragment so
# make targets, scripts, and doctor cannot disagree about the fallback path.
# An environment SEVENDTD_DS_DIR or `make DS=/path ...` override still wins.
DS ?= $(or $(SEVENDTD_DS_DIR),$(shell . "$(ROOT)/scripts/lib/ds_paths.sh" && printf '%s' "$$SEVENDTD_DS_DIR"))
UV := env UV_CACHE_DIR=$(ROOT)/.uv-cache uv run --project $(ROOT)

.DEFAULT_GOAL := help

.PHONY: help test lint lint-shell check-bt format format-check typecheck check lint-html lint-webui clean bridge-build bridge-install bridge-uninstall package sbom
help:
	@echo "7dtd-apm contributor targets (requires: Python 3.11+, uv, Linux):"
	@echo "  make test           pytest suite + version gate (~3s)"
	@echo "                      single test: uv run pytest tools/apm_suite/tests/test_core.py -k name"
	@echo "  make lint           ruff over tools/, scripts/, plans/"
	@echo "  make lint-shell     shellcheck (needs the shellcheck binary)"
	@echo "  make lint-html      Nu HTML checker over rendered reports (needs npx + java)"
	@echo "  make lint-webui     tsc + oxlint + bundle freshness (needs npx)"
	@echo "  make format         ruff format tools/ scripts/ plans/   |   make format-check to verify"
	@echo "  make typecheck      mypy strict"
	@echo "  make check          full local gate = all of the above + check-bt"
	@echo "  make check-ci       exactly what CI runs (= check minus check-bt)"
	@echo "  make check-bt       bpftrace --dry-run over every probe (needs bpftrace + sudo -n; skips visibly)"
	@echo "  make bridge-build   build bridge DLL + WebMod (needs dotnet SDK + npx)"
	@echo "  make bridge-install DS=/path/to/server   build + install into Mods/"
	@echo "  make bridge-uninstall DS=/path/to/server"
	@echo "  make package        release zip under dist/"
	@echo "  make sbom           hash-pinned production dependency inventory under dist/"
	@echo "  make clean          remove caches, venv, dist, bridge build output"
test:
	$(UV) pytest
	$(UV) python scripts/check_version.py
lint:
	$(UV) ruff check tools scripts plans
lint-shell:
	@command -v shellcheck >/dev/null 2>&1 || { \
	  echo "ERROR: shellcheck not found; install it (apt install shellcheck / brew install shellcheck)" >&2; exit 1; }
	shellcheck scripts/*.sh scripts/lib/*.sh tools/apm/*.sh tools/apm/collectors/*.sh tools/host_profiler/*.sh
lint-html:
	./scripts/lint-html.sh
lint-webui:
	./scripts/lint-webui.sh
check-bt:
	./scripts/check_bt.sh
format:
	$(UV) ruff format tools scripts plans
format-check:
	$(UV) ruff format --check tools scripts plans
typecheck:
	$(UV) mypy
check: lint lint-shell lint-html lint-webui format-check typecheck test check-bt
# CI variant: GitHub Actions runners cannot validate bpftrace probes (no host
# kernel access), so check-bt stays a local gate.
check-ci: lint lint-shell lint-html lint-webui format-check typecheck test
bridge-build:
	chmod +x scripts/build_bridge.sh
	./scripts/build_bridge.sh
bridge-install:
	chmod +x scripts/build_bridge.sh scripts/install_bridge.sh
	SEVENDTD_DS_DIR="$(DS)" ./scripts/install_bridge.sh
bridge-uninstall:
	rm -rf "$(DS)/Mods/7dtd-apm-bridge"
package:
	chmod +x scripts/build_bridge.sh scripts/package.sh
	./scripts/package.sh
# Dependency inventory for releases and vuln scanners: name==version plus the
# sha256 of every artifact uv.lock pins, production deps only (no dev group).
sbom:
	mkdir -p $(ROOT)/dist
	env UV_CACHE_DIR=$(ROOT)/.uv-cache uv export --project $(ROOT) \
	  --format requirements-txt --no-emit-project --no-header --no-dev \
	  -o $(ROOT)/dist/sbom-python.txt
	@echo "SBOM -> dist/sbom-python.txt"
clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache .uv-cache .venv dist
	find tools -type d -name __pycache__ -prune -exec rm -rf {} +
	find tools -type f -name '*.py[co]' -delete
	rm -rf bridge/ApmBridge/bin bridge/ApmBridge/obj
