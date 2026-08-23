"""Zombie scale-ladder: spawn zombie tiers near the joined bot cohort and
capture a deep APM session per tier, so the per-entity cost curve and stall
signals can be read off csharp_bridge.json attribution.

Assumes a bot cohort is already joined (7dtd-loadgen --join, no self-spawn) on
a FRESH world save (accumulated ghosts / spawn-drift break spawning; see
7dtd-apm TODO R21). Spawns via AIDirector scouts, which find their own ground
near each player.
"""

import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

APM = Path(__file__).resolve().parent.parent  # 7dtd-apm repo root (plans/..)
HOST, PORT = "127.0.0.1", 8081
PASSWORD = os.environ.get("SEVENDTD_TELNET_PASSWORD", "")
TIERS = [100, 300, 600, 1000]
SNAPSHOT = Path.home() / (
    ".local/share/Steam/steamapps/common/7 Days to Die Dedicated Server"
    "/Mods/7dtd-apm-bridge/telemetry/apm_app_latest.json"
)


def telnet(commands: list[str], read_seconds: float = 2.0) -> str:
    chunks: list[bytes] = []
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.settimeout(0.4)

        def drain(seconds: float) -> None:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                try:
                    data = sock.recv(65536)
                    if data:
                        chunks.append(data)
                except (TimeoutError, OSError):
                    pass

        drain(0.6)
        sock.sendall((PASSWORD + "\n").encode())
        drain(0.4)
        for i, command in enumerate(commands):
            sock.sendall((command + "\n").encode())
            time.sleep(0.04)
            if i % 40 == 39:
                drain(0.2)
        drain(read_seconds)
        sock.sendall(b"exit\n")
    return b"".join(chunks).decode("utf-8", "replace")


def player_ids() -> list[int]:
    return [int(m) for m in re.findall(r"\d+\. id=(\d+),", telnet(["listplayers"]))]


def alive() -> int:
    telnet(["apm dump"])
    time.sleep(2)
    try:
        return int(
            (json.loads(SNAPSHOT.read_text(encoding="utf-8")).get("world") or {}).get(
                "entityAlives"
            )
            or 0
        )
    except (json.JSONDecodeError, OSError, ValueError):
        return -1


def spawn_to(target: int) -> int:
    current = alive()
    rounds = 0
    while current < target and rounds < 40:
        ids = player_ids()
        if len(ids) < 5:
            time.sleep(15)
            rounds += 1
            continue
        # Scout hordes despawn after completing their wave (plateau ~70). Mix
        # in spawnentity (persistent) to actually accumulate higher tiers; both
        # need valid ground near a grounded player (fresh world - see TODO R21).
        commands = [f"spawnscouts {pid}" for pid in ids for _ in range(2)]
        commands += [f"spawnentity {pid} zombieBoe" for pid in ids for _ in range(2)]
        telnet(commands, read_seconds=3)
        rounds += 1
        time.sleep(8)
        current = alive()
        print(f"    tier {target}: alive={current} round={rounds}", flush=True)
    return current


def newest_session() -> Path:
    root = Path.home() / ".local/share/7dtd-apm"
    return max(root.glob("session_*"), key=lambda p: p.stat().st_mtime)


def main() -> int:
    if not PASSWORD:
        print("SEVENDTD_TELNET_PASSWORD is required (passwords are never stored here)", flush=True)
        return 1
    ids = player_ids()
    print(f"players joined: {len(ids)}", flush=True)
    if len(ids) < 10:
        print("need >=10 joined bots; abort")
        return 1
    for tier in TIERS:
        print(f"=== TIER {tier} ===", flush=True)
        reached = spawn_to(tier)
        print(f"  alive={reached}; settling", flush=True)
        time.sleep(8)
        result = subprocess.run(
            [
                "uv",
                "run",
                "7dtd-apm",
                "capture",
                "--seconds",
                "90",
                "--only",
                "all",
                "--reset-bridge",
            ],
            cwd=APM,
            env={
                **os.environ,
                "UV_CACHE_DIR": str(APM / ".uv-cache"),
            },
            check=False,
        )
        session = newest_session()
        (session / "workload.json").write_text(
            json.dumps(
                {
                    "schema": "7dtd.loadgen.run.v1",
                    "label": f"ladder-{tier}",
                    "mode": "ladder",
                    "target": {"host": HOST, "port": 26902},
                    "workload": {
                        "clients": len(ids),
                        "zombieTarget": tier,
                        "zombieAliveAtStart": reached,
                        "zombieAliveAtEnd": alive(),
                        "botMode": "wander",
                    },
                    "result": {"exitCode": result.returncode, "passed": True},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"  TIER {tier}: session={session.name} rc={result.returncode}", flush=True)
    print("LADDER COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
