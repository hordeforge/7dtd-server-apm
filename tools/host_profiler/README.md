# Host perf / bpftrace helpers

Private backends invoked by `uv run 7dtd-server-apm capture` and the flame commands.
Complements the optional
managed bridge (`bridge/README.md`, `docs/APM_CS_BRIDGE.md`). Host samples show
native CPU, GC, IO, scheduler, and syscalls; C# method names require the bridge.

## Prefer the CLI

```bash
# dedicated server must be running
./tools/host_profiler/find_server.sh

uv run 7dtd-server-apm capture --seconds 30
uv run 7dtd-server-apm capture --only cpu,gc --seconds 45
uv run 7dtd-server-apm doctor
```

Sessions land under `~/.local/share/7dtd-server-apm/session_*` (override with
`SEVENDTD_APM_DIR`). Do not write captures into the git tree.

## Modules (collector adapters)

| Module | Tool | Needs root | What you get |
|--------|------|------------|--------------|
| `proc` | `/proc` poller | no | CPU%, RSS, threads, fds, disk B/s, ctx switches |
| `perf` | `perf record -g` | usually no | native call graph + flame artifacts |
| `cpu` | bpftrace profile | yes (`sudo -n`) | on-CPU `ustack` histogram |
| `offcpu` | bpftrace sched | yes | blocked time / sleep stacks |
| `runqlat` | bpftrace sched | yes | wakeup to run latency |
| `syscalls` | bpftrace | yes | syscall counts + latency |
| `io` | bpftrace | yes | VFS/TCP/pagefaults + top `openat` paths |
| `gc` | bpftrace uprobe | yes | `mono_gc_collect` / `GC_gcollect` latency |

`TARGET_COMM` is `7DaysToDieServe` (kernel 15-char `comm`).

## Direct tool use (debug only)

```bash
uv run python tools/host_profiler/proc_sample.py --seconds 60 --threads --json /tmp/p.jsonl
./tools/host_profiler/perf_record.sh /tmp/perf 30
sudo -n bpftrace -D TARGET_PID=$(./tools/host_profiler/find_server.sh) \
  -D 'TARGET_COMM="7DaysToDieServe"' tools/host_profiler/scripts/cpu_profile.bt
```

## Correlate host capture with bridge evidence

```bash
# Prefer structured session analysis:
uv run 7dtd-server-apm audit SESSION
uv run 7dtd-server-apm bridge SESSION

# Low-level helper (expects a capture dir with proc.jsonl):
uv run python tools/host_profiler/correlate.py \
  --capture ~/.local/share/7dtd-server-apm/session_... \
  --game-log /path/to/server/output_log.txt
```

`correlate.py` still understands legacy EfficientServer SPIKE log lines if present;
current instrumentation is the APM bridge snapshot, not in-mod profiler logs.

## Interpreting native stacks

Unity dedicated is **Mono + UnityPlayer.so**. Expect:

| Frame | Meaning |
|-------|---------|
| `libmonobdwgc` / `mono_gc_collect` / `GC_gcollect` | managed GC pause |
| `UnityPlayer.so` | engine loop / jobs / physics |
| `pthread_cond_wait` / `futex` in off-CPU | waiting on worker or lock |
| high `runqlat` | not enough CPU / oversubscribed |
| many `openat` on save/region paths | disk/world streaming pressure |
| multi-core `cpu%` > 100 in proc_sample | expected (server uses threads) |

C# method names will **not** appear here; use the APM bridge snapshot / deep mode.

## Privileges

bpftrace needs `CAP_BPF` / root. This host allows `sudo -n bpftrace`.
If sudo is unavailable, use `--only proc,perf` (or layers `doctor` reports available).

```bash
# one-time if needed
sudo setcap cap_bpf,cap_perfmon,cap_sys_resource+ep $(which bpftrace)
```
