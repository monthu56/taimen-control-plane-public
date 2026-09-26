"""Process-local low-cardinality counters (v0.4 observability).

A deliberately tiny substitute for a metrics client: named monotonic
counters, exported by ``GET /metrics`` in Prometheus text format alongside
the HTTP middleware counters and a handful of DB-derived gauges. No tenant /
task / run identifiers ever become label values.
"""

from collections import defaultdict

_counters: defaultdict[str, float] = defaultdict(float)


def inc(name: str, value: float = 1.0) -> None:
    _counters[name] += value


def counters() -> dict[str, float]:
    return dict(_counters)


def reset() -> None:  # for tests
    _counters.clear()
