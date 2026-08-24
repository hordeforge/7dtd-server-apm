from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

from .io import file_sha256
from .paths import REPO, apm_root, dedicated_dir
from .session import keep_sessions_budget, prune_grace_hours


def _command(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {"ok": path is not None, "path": path, "fix": None if path else f"install {name}"}


def _sudo() -> dict[str, Any]:
    path = shutil.which("sudo")
    if path is None:
        return {"ok": False, "fix": "install sudo and configure narrowly scoped APM rules"}
    try:
        result = subprocess.run([path, "-n", "true"], capture_output=True, timeout=3, check=False)
    except subprocess.TimeoutExpired:
        # The timeout exists precisely for a hung sudo; reporting it must not
        # crash the whole doctor run with an unhandled TimeoutExpired.
        return {
            "ok": False,
            "fix": "sudo -n true timed out after 3s; check sudo/PAM configuration",
        }
    return {
        "ok": result.returncode == 0,
        "fix": None
        if result.returncode == 0
        else "allow non-interactive bpftrace, mount, and umount for the APM user",
    }


def _telnet(host: str, port: int) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            # Reachability is not authentication: a bare TCP connect succeeds for
            # anything listening on the port, so a green here does not prove the
            # telnet password is accepted. Label it so app_sim is not a false green.
            return {
                "ok": True,
                "endpoint": f"{host}:{port}",
                "note": "port reachable; telnet auth not verified (reachable != authenticated)",
            }
    except OSError as error:
        return {"ok": False, "endpoint": f"{host}:{port}", "fix": str(error)}


def _existing_ancestor(path: Path) -> Path:
    """Nearest existing directory at or above `path`. apm_root() may not exist
    before the first capture, and disk_usage() requires a real path."""
    path = path.resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _bridge_status() -> dict[str, Any]:
    """Installed bridge vs local build: stale DLLs measure with old hooks."""
    installed = dedicated_dir() / "Mods/7dtd-server-apm-bridge/7dtd-server-apm-bridge.dll"
    built = REPO / "dist/7dtd-server-apm-bridge/7dtd-server-apm-bridge.dll"
    if not installed.is_file():
        return {"ok": False, "fix": "make bridge-install (bridge not installed)"}
    result: dict[str, Any] = {"ok": True, "fix": None}
    if built.is_file():
        # The server (and its Mods dir) commonly runs as another user; an
        # unreadable installed DLL is a diagnosable condition, not a reason to
        # crash the whole doctor report.
        try:
            differs = file_sha256(installed) != file_sha256(built)
        except OSError as error:
            return {
                "ok": False,
                "fix": f"cannot hash the installed bridge ({error}); check permissions",
            }
        if differs:
            result = {
                "ok": False,
                "fix": "installed bridge differs from dist build; make bridge-install + restart server",
            }
    config = dedicated_dir() / "Mods/7dtd-server-apm-bridge/Config/apmbridge.json"
    settings: Any = None
    with suppress(OSError, ValueError):
        settings = json.loads(config.read_text(encoding="utf-8"))
    # Valid JSON that is not an object (a hand-edited "[1,2]") has no .get;
    # like every other malformed config read here it is a diagnosable
    # condition, not a reason to crash the whole doctor report.
    if isinstance(settings, dict):
        result["deep_mode"] = bool(settings.get("DeepMode"))
        if not settings.get("DeepMode"):
            result["fix"] = (
                result["fix"] or "DeepMode off: per-entity AI/path sections will not be measured"
            )
    return result


def _mono_gc_probe(pid: int | None) -> dict[str, Any]:
    """The gross-allocation + stop-the-world probes uprobe libmonobdwgc-2.0.so.
    They can only attach if that library is mapped into the target process."""
    if pid is None:
        return {"ok": False, "fix": "no target pid; GC/alloc uprobes unavailable"}
    try:
        maps = Path(f"/proc/{pid}/maps").read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return {"ok": False, "fix": f"cannot read /proc/{pid}/maps: {error}"}
    if "libmonobdwgc-2.0.so" in maps:
        return {"ok": True, "fix": None}
    return {
        "ok": False,
        "fix": "libmonobdwgc-2.0.so not mapped; GC gross-alloc/STW probes disabled",
    }


def inspect(pid: int | None, host: str, port: int) -> dict[str, Any]:
    # Imported here, not at module level, so every non-doctor CLI command skips
    # the ~70ms psutil import on startup.
    import psutil

    candidates: list[dict[str, Any]] = []
    if pid is None:
        for process in psutil.process_iter(("pid", "name")):
            if str(process.info.get("name") or "").startswith("7DaysToDieServe"):
                candidates.append(
                    {"pid": int(process.info["pid"]), "name": process.info.get("name")}
                )
        if len(candidates) == 1:
            pid = int(candidates[0]["pid"])
    process_ok = pid is not None and psutil.pid_exists(pid)
    checks = {name: _command(name) for name in ("perf", "bpftrace", "timeout")}
    checks["sudo"] = _sudo()
    checks["telnet"] = _telnet(host, port)
    checks["bridge"] = _bridge_status()
    checks["mono_gc_probe"] = _mono_gc_probe(pid)
    checks["target"] = {
        "ok": process_ok,
        "pid": pid,
        "candidates": candidates,
        "fix": None
        if process_ok
        else (
            "multiple servers found; pass --pid"
            if len(candidates) > 1
            else "start the server or pass --pid"
        ),
    }
    paranoid: int | None = None
    with suppress(OSError, ValueError):
        paranoid = int(
            Path("/proc/sys/kernel/perf_event_paranoid").read_text(encoding="ascii").strip()
        )
    perf_ok = checks["perf"]["ok"] and (paranoid is None or paranoid <= 2 or os.geteuid() == 0)
    ebpf_ok = checks["bpftrace"]["ok"] and checks["sudo"]["ok"] and process_ok
    layers = {
        "app_sim": checks["telnet"]["ok"],
        "cpu": perf_ok and process_ok,
        "memory_cache": perf_ok and process_ok,
        "threads": process_ok,
        "runtime_gc": ebpf_ok,
        "sync_locks": ebpf_ok,
        "scheduler": ebpf_ok,
        "io": ebpf_ok,
    }
    # Captures land under apm_root(), not the cwd; measure the volume that will
    # actually hold the session data.
    disk_free = shutil.disk_usage(_existing_ancestor(apm_root())).free
    # Active runtime configuration so a misread env override is visible without
    # shell archaeology. Resolved through the same helpers capture/prune use so
    # this report cannot drift from actual behavior. The telnet secret is shown
    # only as set/unset - never its value.
    return {
        "schema": "7dtd.apm.doctor.v2",
        "ready": all(layers.values()),
        "available_layers": layers,
        "checks": checks,
        "environment": {
            "apm_root": str(apm_root()),
            "dedicated_dir": str(dedicated_dir()),
            "keep_sessions": keep_sessions_budget(),
            "prune_grace_hours": prune_grace_hours(),
            "telnet_password_set": bool(os.environ.get("SEVENDTD_TELNET_PASSWORD", "")),
        },
        "perf_event_paranoid": paranoid,
        "disk_free_bytes": disk_free,
        "disk_low": disk_free < 5 * 1024**3,  # perf.data can be GBs
    }
