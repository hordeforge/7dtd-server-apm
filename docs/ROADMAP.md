# Suite improvement roadmap (100 items)

**Owns:** APM + loadgen improvement backlog.  
**Not:** shipped capability claims ([FEATURES](FEATURES.md)); do not mark product Done here.

Prioritized ideas for the APM + loadgen suite. `[x]` = done. Grounded in real
gaps found while profiling the 7DTD server (GC churn, player-scale network wall).

## A. Analysis + diagnosis
1. [ ] Per-connection network cost breakdown (`taskSerialize` per player)
2. [x] Detect super-linear (O(N^2)) scaling signature across a ramp
3. [x] Death-spiral detection (tick interval growing monotonically + backlog)
4. [x] Correlate each GC STW pause to its frame spike by timestamp
5. [ ] Memory-leak detection (RSS/heap trend regression over long captures)
6. [ ] Per-thread CPU attribution (which threads burn CPU)
7. [ ] Main-thread starvation vs worker imbalance classifier
8. [ ] Idle-vs-loaded baseline delta in every report
9. [ ] p99/p99.9 tick health (not just avg + late count)
10. [ ] Tick jitter/variance metric
11. [ ] Flag when observer overhead (bridge/telnet) is material
12. [x] Allocation-per-tick metric (gross alloc / tick)
13. [ ] Chunk-thrash detection (same chunks loaded/unloaded repeatedly)
14. [ ] Lock-holder attribution (which section holds contended locks)
15. [ ] Cross-session regression auto-flagging in the index

## B. Host probes (bpftrace / perf)
16. [ ] UDP send-queue depth / backpressure probe
17. [ ] Per-thread off-CPU attribution
18. [ ] Syscall latency histogram (slowest syscalls)
19. [ ] Major page-fault stack attribution
20. [ ] TCP/UDP retransmit + drop counting
21. [ ] Futex holder (not just waiter) tracking
22. [ ] Scheduler migration counting (cache thrash)
23. [ ] mmap/munmap churn tracking
24. [ ] fd-count / leak probe
25. [ ] GC mark-vs-sweep phase split

## C. Bridge (managed instrumentation)
26. [ ] Per-player connection metrics (bytes, packets, queue depth)
27. [ ] Network serialization split by package type
28. [ ] Entity-distribution list size per player
29. [x] Tick-phase breakdown (sim vs net vs save)
30. [ ] Chunk-observer count per client
31. [ ] Managed thread-pool queue depth
32. [ ] Boehm GC callback hooks for exact pause boundaries
33. [ ] Pathfinding queue depth (enqueue/drain/compute)
34. [x] Per-tick gross-allocation counter (GC_get_total_bytes delta/tick)
35. [ ] Cut the bridge's own export allocation (observer effect)

## D. CLI / UX
36. [x] Milestone capture ramp (`scenario matrix` plan + `scaling` fit)
37. [ ] `capture --follow` streams metrics during capture
38. [ ] `compare --attribution` diffs two captures' subsystem shares
39. [ ] `watch` alias for monitor
40. [ ] `capture --auto-alloc` enables alloc probe when GC detected
41. [ ] Clearer probe-failure messages
42. [ ] `doctor --explain` on each failing check
43. [ ] Capture progress indicator
44. [ ] `capture --tag k=v` arbitrary metadata
45. [ ] Shell completion

## E. Reporting / visualization
46. [ ] Allocation flamegraph (from mono_alloc stacks)
47. [ ] Tick-duration timeline chart
48. [ ] Per-subsystem trend across the window
49. [ ] Sortable section table in the HTML report
50. [ ] Chrome/Perfetto trace export
51. [ ] Sparklines for key metrics
52. [ ] Subsystem-colored flamegraph
53. [ ] Two-session side-by-side report
54. [ ] Prometheus histograms (tick, alloc), not only gauges
55. [ ] Grafana dashboard JSON

## F. Budget / CI
56. [ ] Budget on network subsystem share
57. [ ] Budget on p99 tick
58. [ ] Budget on per-player cost (ms/player)
59. [ ] Auto-baseline from a golden session
60. [ ] Named budget profiles per scenario

## G. Loadgen behaviors
61. [ ] Builder mode (block place - needs SetBlock packet)
62. [ ] Miner mode (sustained block damage - needs block-damage packet)
63. [ ] Looter mode (container interact - needs observer + interact)
64. [ ] Trader mode
65. [ ] Vehicle-driver mode (mount + drive)
66. [ ] Sprint bursts (stamina)
67. [ ] Day/night activity cycle
68. [ ] Party/group clustering
69. [x] Base-defender (stationary near a point) = bait
70. [ ] Gathering (harvest plants/resources)

## H. Loadgen protocol fidelity
71. [ ] Periodic player stats (health/stamina) - needs format
72. [ ] Chunk ACK / entity-observer registration (see entities)
73. [ ] EntitySpawn parsing (know nearby entities)
74. [ ] Ping/keepalive realism
75. [x] Non-lockstep packet timing (deterministic jitter)
76. [ ] Inventory sync packets
77. [ ] Buff/status packets
78. [ ] Clean disconnect handshake
79. [ ] Reconnect with saved state
80. [ ] Emote packets

## I. Orchestration / scale
81. [x] Scale-ladder runner (ramp to N, capture per level)
82. [ ] Distributed loadgen across hosts
83. [x] Bot loopback-IP diversity
84. [ ] Graceful ramp-down
85. [x] Per-bot RTT export
86. [x] Coordinated horde waves
87. [x] JSON spawn/mix schedules
88. [ ] Bot crash recovery / auto-rejoin (have rejoin)
89. [ ] Live add/remove bots during a run
90. [ ] Resource-aware bot capping

## J. Reproducibility / testing
91. [ ] Deterministic server-side spawn (fixed spawn RNG)
92. [ ] Golden-metric regression tests
93. [x] World snapshot/reset for clean state
94. [x] Seed bots + world deterministically
95. [ ] CI job running the canonical profile
96. [ ] Fuzz the packet codec (round-trip)
97. [ ] Property tests for the analysis
98. [ ] Bridge C# unit tests
99. [ ] Metric-stability tests (variance bounds)
100. [ ] End-to-end smoke test in CI
