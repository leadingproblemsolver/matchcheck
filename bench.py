#!/usr/bin/env python3
"""Scaling evidence: wall time and hash ops at growing sizes vs a nested-loop match."""
import matchcheck as mc
print("%9s %12s %14s %10s %10s" % ("lines", "hash_ops", "naive_cmp", "ms", "us/line"))
prev = None
for n in (1_000, 10_000, 100_000, 400_000):
    data = mc.synth(n)
    t = mc.evaluate(data)["telemetry"]
    print("%9d %12d %14d %10.1f %10.2f" % (n, t["hash_ops"], t["naive_comparisons"], t["elapsed_ms"], t["elapsed_ms"] * 1000 / n))
