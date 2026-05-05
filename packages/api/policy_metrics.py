"""Lightweight metrics for the server-side Policy Engine.

Two consumers:
  - operators (curl /v1/policy/metrics → JSON)
  - alerting (the dashboard's nav pill polls counters)

Stays in-process — a Prometheus client + scrape pipeline is the right
v2 answer, but for the production-ready milestone we just need
visibility into eval rate, violation rate, latency tail, and engine
health. Zero external deps.

Thread-safety: every public function takes the module lock. Hot
path (record_eval) is two list ops + one dict update under the
lock — fine for the QPS we're shipping at.
"""

from __future__ import annotations

import bisect
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Dict, List

_lock = threading.Lock()

# Cumulative counters
_evals_total: Counter[str] = Counter()         # by trigger (span_end / trace_end)
_violations_total: Counter[str] = Counter()    # by f"{policy_name}/{severity}"
_errors_total: Counter[str] = Counter()        # by kind (config / eval / dispatcher)

# Latency samples — last N evaluations. Bounded so the list doesn't grow
# unbounded; that's enough to compute a stable p50/p99 across a 5-min
# window at any realistic QPS.
_LATENCY_WINDOW = 2_000
_eval_latencies_ms: List[float] = []

# Engine health snapshot
_engine_loaded: bool = False
_policies_count: int = 0
_loaded_at: float = 0.0       # epoch seconds when engine last loaded
_loaded_mtime: float = 0.0    # the policy file's mtime at load time
_loaded_path: str = ""


# --- public recorders -------------------------------------------------------


def record_eval(trigger: str, duration_ms: float, violations_fired: int = 0) -> None:
    """Called on every evaluate_span / evaluate_trace pass."""
    with _lock:
        _evals_total[trigger] += 1
        _eval_latencies_ms.append(duration_ms)
        if len(_eval_latencies_ms) > _LATENCY_WINDOW:
            # Drop oldest in bulk to amortize the cost
            del _eval_latencies_ms[: len(_eval_latencies_ms) - _LATENCY_WINDOW]


def record_violation(policy_name: str, severity: str) -> None:
    with _lock:
        _violations_total[f"{policy_name}/{severity}"] += 1


def record_error(kind: str) -> None:
    """`kind` is short — 'config', 'eval', 'dispatcher', etc."""
    with _lock:
        _errors_total[kind] += 1


def set_engine_state(
    loaded: bool,
    policies_count: int = 0,
    path: str = "",
    mtime: float = 0.0,
) -> None:
    """Called by policy_runtime.get_engine() and reload_engine()."""
    global _engine_loaded, _policies_count, _loaded_at, _loaded_mtime, _loaded_path
    with _lock:
        _engine_loaded = loaded
        _policies_count = policies_count
        _loaded_at = time.time() if loaded else 0.0
        _loaded_mtime = mtime
        _loaded_path = path


# --- snapshot ---------------------------------------------------------------


def _percentile(samples: List[float], p: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    idx = int(len(s) * p)
    if idx >= len(s):
        idx = len(s) - 1
    return s[idx]


@dataclass
class MetricsSnapshot:
    engine_loaded: bool
    policies_count: int
    loaded_at: float
    loaded_mtime: float
    loaded_path: str
    evals_total: Dict[str, int]
    violations_total: Dict[str, int]
    errors_total: Dict[str, int]
    eval_latency_ms_p50: float
    eval_latency_ms_p99: float
    eval_latency_ms_max: float
    eval_latency_samples: int

    def to_dict(self) -> dict:
        return asdict(self)


def snapshot() -> MetricsSnapshot:
    """Atomic read of all counters + p50/p99 over the rolling window."""
    with _lock:
        latencies = list(_eval_latencies_ms)
        return MetricsSnapshot(
            engine_loaded=_engine_loaded,
            policies_count=_policies_count,
            loaded_at=_loaded_at,
            loaded_mtime=_loaded_mtime,
            loaded_path=_loaded_path,
            evals_total=dict(_evals_total),
            violations_total=dict(_violations_total),
            errors_total=dict(_errors_total),
            eval_latency_ms_p50=_percentile(latencies, 0.50),
            eval_latency_ms_p99=_percentile(latencies, 0.99),
            eval_latency_ms_max=max(latencies) if latencies else 0.0,
            eval_latency_samples=len(latencies),
        )


def reset() -> None:
    """For tests only — wipe all state."""
    global _engine_loaded, _policies_count, _loaded_at, _loaded_mtime, _loaded_path
    with _lock:
        _evals_total.clear()
        _violations_total.clear()
        _errors_total.clear()
        _eval_latencies_ms.clear()
        _engine_loaded = False
        _policies_count = 0
        _loaded_at = 0.0
        _loaded_mtime = 0.0
        _loaded_path = ""
