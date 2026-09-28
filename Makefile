ROOT := $(CURDIR)
# Resolve the dedicated-server default through the shared shell fragment so
# make targets, scripts, and doctor cannot disagree about the fallback path.
# An environment SEVENDTD_DS_DIR or `make DS=/path ...` override still wins.
DS ?= $(or $(SEVENDTD_DS_DIR),$(shell . "$(ROOT)/scripts/lib/ds_paths.sh" && printf '%s' "$$SEVENDTD_DS_DIR"))
# --locked makes every target fail instead of silently re-locking when
# pyproject.toml drifted from uv.lock; dependency updates must go through
# `uv lock` explicitly.
UV := env UV_CACHE_DIR=$(ROOT)/.uv-cache uv run --locked --project $(ROOT)

.DEFAULT_GOAL := help

.PHONY: help test coverage lint lint-shell check-bt format format-check typecheck check check-ci lint-html lint-webui clean bridge-build bridge-install bridge-uninstall package sbom
help:
	@echo "7dtd-server-apm contributor targets (requires: Python 3.11+, uv, Linux):"
	@echo "  make test           pytest suite + version gate (~1min)"
	@echo "                      single test: uv run pytest tools/apm_suite/tests/test_core.py -k name"
	@echo "  make lint           ruff over tools/, scripts/, plans/, tests/"
	@echo "  make lint-shell     shellcheck (needs the shellcheck binary)"
	@echo "  make lint-html      Nu HTML checker over rendered reports (needs bunx + java)"
	@echo "  make lint-webui     tsc + oxlint + bundle freshness (needs bunx)"
	@echo "  make format         ruff format tools/ scripts/ plans/ tests/   |   make format-check to verify"
	@echo "  make typecheck      mypy strict"
	@echo "  make check          full local gate = all of the above + check-bt"
	@echo "  make check-ci       exactly what CI runs (= check minus check-bt)"
	@echo "  make check-bt       bpftrace --dry-run over every probe (needs bpftrace + sudo -n; skips visibly)"
	@echo "  make bridge-build   build bridge DLL + WebMod (needs dotnet SDK + bunx)"
	@echo "  make bridge-install DS=/path/to/server   build + install into Mods/"
	@echo "  make bridge-uninstall DS=/path/to/server"
	@echo "  make package        release zip under dist/"
	@echo "  make sbom           hash-pinned + CycloneDX production dependency inventories under dist/"
	@echo "  make clean          remove caches, venv, dist, bridge build output"
test:
	$(UV) pytest
	$(UV) python scripts/check_version.py

# Line coverage of apm_suite under the pytest suite. Writes .coverage in the
# repo root; CI renders it into the README badge with scripts/coverage_badge.py.
coverage:
	$(UV) pytest --cov=apm_suite --cov-report=term-missing
lint:
	$(UV) ruff check tools scripts plans tests
lint-shell:
	@command -v shellcheck >/dev/null 2>&1 || { \
	  echo "ERROR: shellcheck not found; install it (apt install shellcheck / brew install shellcheck)" >&2; exit 1; }
	@scripts/lib/check_shellcheck_version.sh
	shellcheck scripts/*.sh scripts/lib/*.sh tools/apm/collectors/*.sh tools/host_profiler/*.sh
lint-html:
	./scripts/lint-html.sh
lint-webui:
	./scripts/lint-webui.sh
check-bt:
	./scripts/check_bt.sh
format:
	$(UV) ruff format tools scripts plans tests
format-check:
	$(UV) ruff format --check tools scripts plans tests
typecheck:
	$(UV) mypy
check: lint lint-shell lint-html lint-webui format-check typecheck test check-bt
# CI variant: GitHub Actions runners cannot validate bpftrace probes (no host
# kernel access), so check-bt stays a local gate.
check-ci: lint lint-shell lint-html lint-webui format-check typecheck test
# Every script these targets call is committed with its exec bit set (git mode
# 100755), so a target never chmods the source tree on its way to building.
bridge-build:
	./scripts/build_bridge.sh
bridge-install:
	SEVENDTD_DS_DIR="$(DS)" ./scripts/install_bridge.sh
bridge-uninstall:
	@# The tuned config is the one file in the mod folder the operator cannot
	@# regenerate: the release zip ships apmbridge.json.example, not a live
	@# config. Move it beside the mod dir and print where it went instead of
	@# letting rm -rf take the settings with the code. A reinstall seeds a
	@# fresh factory config, so a second uninstall must not overwrite the
	@# config the first one saved: take the next free name instead.
	@cfg="$(DS)/Mods/7dtd-server-apm-bridge/Config/apmbridge.json"; \
	saved="$(DS)/Mods/7dtd-server-apm-bridge-config.json"; \
	if [ -f "$$cfg" ]; then \
	  dest="$$saved"; n=1; \
	  while [ -e "$$dest" ]; do dest="$$saved.$$n"; n=$$((n + 1)); done; \
	  mv "$$cfg" "$$dest"; \
	  echo "config kept -> $$dest"; \
	fi
	rm -rf "$(DS)/Mods/7dtd-server-apm-bridge"
	@echo "Uninstalled. Reinstall, then move the saved config back to Mods/7dtd-server-apm-bridge/Config/apmbridge.json to keep your settings."
package:
	./scripts/package.sh
# Dependency inventory for releases and vuln scanners, production deps only
# (no dev group). Two formats from the same locked resolution: the
# requirements-txt export carries name==version plus the sha256 of every
# artifact uv.lock pins; the CycloneDX 1.5 export is the standard BOM shape
# (purl + dependency graph) that scanners ingest directly.
sbom:
	mkdir -p $(ROOT)/dist
	env UV_CACHE_DIR=$(ROOT)/.uv-cache uv export --project $(ROOT) --locked \
	  --format requirements-txt --no-emit-project --no-header --no-dev \
	  -o $(ROOT)/dist/sbom-python.txt
	env UV_CACHE_DIR=$(ROOT)/.uv-cache uv export --project $(ROOT) --locked \
	  --preview-features sbom-export --format cyclonedx1.5 --no-emit-project --no-dev \
	  -o $(ROOT)/dist/sbom-python.cdx.json
	@echo "SBOM -> dist/sbom-python.txt + dist/sbom-python.cdx.json"
clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache .uv-cache .venv dist
	find tools -type d -name __pycache__ -prune -exec rm -rf {} +
	find tools -type f -name '*.py[co]' -delete
	rm -rf bridge/ApmBridge/bin bridge/ApmBridge/obj
