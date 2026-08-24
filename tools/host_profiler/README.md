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

Names are the `--only` tokens from the shared catalog (`tools/apm_suite/collectors.py`).

| Collector | Backend | Root | What you get |
|--------|------|------------|--------------|
| `threads` (+ `proc` ride-along) | `/proc` pollers | no | per-thread CPU/state samples; RSS, fds, disk B/s, ctx switches |
| `hw` | `perf stat` | usually no | PMU counters: cycles/IPC, cache misses, stalls, faults |
| `perf` | `perf record --call-graph fp` | usually no | folded stacks + flame artifacts (frame pointers unwind through Mono JIT) |
| `oncpu` | bpftrace profile (`scripts/cpu_profile.bt`) | yes (`sudo -n`) | on-CPU `ustack` histogram |
| `offcpu` | bpftrace sched (`scripts/offcpu.bt`) | yes | main-thread blocked time split by sleep vs disk state |
| `runqlat` | bpftrace sched (`scripts/runqlat.bt`) | yes | wakeup-to-run latency |
| `states` | bpftrace sched (`../apm/collectors/sched_states.bt`) | yes | thread-state time + main-thread run-queue latency |
| `futex` | bpftrace tracepoint (`../apm/collectors/futex.bt`) | yes | futex wait counts + main-thread wait share |
| `vfs` / `block` / `io_net` | bpftrace (`../apm/collectors/vfs_lat.bt`, `block_lat.bt`; `scripts/io_net.bt`) | yes | VFS/block latency, slow main-thread file IO, TCP/UDP byte sums |
| `mono_gc` | bpftrace uprobe (`scripts/mono_gc.bt`) | yes | GC collect latency + `GC_stop_world`/`GC_start_world` freeze timing |
| `mono_alloc` | bpftrace uprobe (`../apm/collectors/mono_alloc.bt`) | yes | gross allocation churn + sampled allocation stacks (opt-in token `alloc`) |

`TARGET_COMM` is `7DaysToDieServe` (kernel 15-char `comm`).

## Direct tool use (debug only)

```bash
uv run python tools/host_profiler/proc_sample.py --seconds 60 --threads --json /tmp/p.jsonl
./tools/host_profiler/perf_record.sh /tmp/perf 30
# The .bt scripts read TARGET_PID/TARGET_COMM from #defines, so preprocess
# them (as capture does) before running bpftrace directly:
uv run python tools/host_profiler/preprocess_bt.py \
  tools/host_profiler/scripts/cpu_profile.bt -o /tmp/cpu_profile.bt \
  --pid "$(./tools/host_profiler/find_server.sh)" --comm 7DaysToDieServe
sudo -n bpftrace /tmp/cpu_profile.bt
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
