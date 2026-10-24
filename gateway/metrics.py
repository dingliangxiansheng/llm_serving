"""极简 Prometheus 指标，不引入 prometheus_client。"""

from __future__ import annotations

import threading
from collections import Counter


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self._tokens = Counter()  # direction -> count
        self._latency_sum = 0.0
        self._latency_count = 0

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        with self._lock:
            self._counters[key] += value

    def observe_latency(self, seconds: float) -> None:
        with self._lock:
            self._latency_sum += seconds
            self._latency_count += 1

    def add_tokens(self, direction: str, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self._tokens[direction] += count

    def render(self, gauge: dict[str, float] | None = None) -> str:
        lines: list[str] = []
        with self._lock:
            counters = sorted(self._counters.items())
            tokens = dict(self._tokens)
            latency_sum = self._latency_sum
            latency_count = self._latency_count

        aggregated: Counter[str] = Counter()
        for (name, labels), value in counters:
            label_str = ",".join(f'{k}="{v}"' for k, v in labels)
            key = f"{name}{{{label_str}}}" if label_str else name
            aggregated[key] += value

        for key, value in sorted(aggregated.items()):
            lines.append(f"qw_{key} {value:.0f}")
        for direction, value in sorted(tokens.items()):
            lines.append(f'qw_tokens_total{{direction="{direction}"}} {value}')
        lines.append(f"qw_request_latency_seconds_sum {latency_sum:.3f}")
        lines.append(f"qw_request_latency_seconds_count {latency_count}")
        for name, value in (gauge or {}).items():
            lines.append(f"qw_{name} {value}")
        return "\n".join(lines) + "\n"
