ROOT := $(CURDIR)
DS ?= $(HOME)/.local/share/Steam/steamapps/common/7 Days to Die Dedicated Server
UV := env UV_CACHE_DIR=$(ROOT)/.uv-cache uv run --project $(ROOT)

.PHONY: test lint lint-shell check-bt format format-check typecheck check clean bridge-build bridge-install bridge-uninstall
test:
	$(UV) pytest
lint:
	$(UV) ruff check tools
lint-shell:
	shellcheck scripts/*.sh tools/apm/*.sh tools/apm/collectors/*.sh tools/host_profiler/*.sh
check-bt:
	./scripts/check_bt.sh
format:
	$(UV) ruff format tools
format-check:
	$(UV) ruff format --check tools
typecheck:
	$(UV) mypy
check: lint lint-shell format-check typecheck test check-bt
bridge-build:
	chmod +x scripts/build_bridge.sh
	./scripts/build_bridge.sh
bridge-install:
	chmod +x scripts/build_bridge.sh scripts/install_bridge.sh
	SEVENDTD_DS_DIR="$(DS)" ./scripts/install_bridge.sh
bridge-uninstall:
	rm -rf "$(DS)/Mods/7dtd-apm-bridge"
clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache .uv-cache .venv dist
	find tools -type d -name __pycache__ -prune -exec rm -rf {} +
	find tools -type f -name '*.py[co]' -delete
	rm -rf bridge/ApmBridge/bin bridge/ApmBridge/obj
