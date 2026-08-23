ROOT := $(CURDIR)
# Resolve the dedicated-server default through the shared shell fragment so
# make targets, scripts, and doctor cannot disagree about the fallback path.
# An environment SEVENDTD_DS_DIR or `make DS=/path ...` override still wins.
DS ?= $(or $(SEVENDTD_DS_DIR),$(shell . "$(ROOT)/scripts/lib/ds_paths.sh" && printf '%s' "$$SEVENDTD_DS_DIR"))
UV := env UV_CACHE_DIR=$(ROOT)/.uv-cache uv run --project $(ROOT)

.PHONY: test lint lint-shell check-bt format format-check typecheck check lint-html lint-webui clean bridge-build bridge-install bridge-uninstall package
test:
	$(UV) pytest
	$(UV) python scripts/check_version.py
lint:
	$(UV) ruff check tools
lint-shell:
	shellcheck scripts/*.sh tools/apm/*.sh tools/apm/collectors/*.sh tools/host_profiler/*.sh
lint-html:
	./scripts/lint-html.sh
lint-webui:
	./scripts/lint-webui.sh
check-bt:
	./scripts/check_bt.sh
format:
	$(UV) ruff format tools
format-check:
	$(UV) ruff format --check tools
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
clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache .uv-cache .venv dist
	find tools -type d -name __pycache__ -prune -exec rm -rf {} +
	find tools -type f -name '*.py[co]' -delete
	rm -rf bridge/ApmBridge/bin bridge/ApmBridge/obj
