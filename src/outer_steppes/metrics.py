"""Prometheus metrics (DESIGN.md, Observability), served on the admin listener.

A deliberately small, dependency-free implementation of the text exposition
format. Label values are drawn from fixed sets (command names, result names,
status classes), never from ids, tokens, URLs, or prompts.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60)
WAIT_BUCKETS = (1, 5, 10, 30, 60, 120, 300, 600, 900)


def _labels(names: Sequence[str], values: Sequence[str]) -> str:
    if not names:
        return ""
    escaped = (str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") for v in values)
    return "{" + ",".join(f'{n}="{v}"' for n, v in zip(names, escaped)) + "}"


class Counter:
    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> None:
        self.name, self.help, self.label_names = name, help_text, tuple(label_names)
        self.values: dict[tuple[str, ...], float] = {}

    def inc(self, *label_values: str, amount: float = 1) -> None:
        key = tuple(label_values)
        self.values[key] = self.values.get(key, 0) + amount

    def value(self, *label_values: str) -> float:
        return self.values.get(tuple(label_values), 0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for key, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_labels(self.label_names, key)} {value:g}")
        return lines


class Histogram:
    def __init__(self, name: str, help_text: str, buckets: Sequence[float]) -> None:
        self.name, self.help, self.buckets = name, help_text, tuple(buckets)
        self.counts = [0] * len(self.buckets)
        self.count = 0
        self.sum = 0.0

    def observe(self, value: float) -> None:
        index = bisect.bisect_left(self.buckets, value)
        if index < len(self.counts):
            self.counts[index] += 1
        self.count += 1
        self.sum += value

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        cumulative = 0
        for bound, count in zip(self.buckets, self.counts):
            cumulative += count
            lines.append(f'{self.name}_bucket{{le="{bound:g}"}} {cumulative}')
        lines += [f'{self.name}_bucket{{le="+Inf"}} {self.count}',
                  f"{self.name}_sum {self.sum:g}", f"{self.name}_count {self.count}"]
        return lines


class Gauge:
    def __init__(self, name: str, help_text: str, read: Callable[[], float] = lambda: 0) -> None:
        self.name, self.help, self.read = name, help_text, read

    def render(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge", f"{self.name} {self.read():g}"]


class Registry:
    def __init__(self) -> None:
        self.interactions = Counter("bridge_interactions_total", "Signed interactions by command and outcome.",
                                    ("command", "result"))
        self.ack_latency = Histogram("bridge_ack_latency_seconds", "Time to answer POST /interactions.",
                                     LATENCY_BUCKETS)
        self.automation_posts = Counter("bridge_automation_post_total", "Signed automation POSTs by outcome.",
                                        ("result",))
        self.automation_latency = Histogram("bridge_automation_latency_seconds",
                                            "Duration of the signed automation POST.", LATENCY_BUCKETS)
        self.replies = Counter("bridge_reply_total", "Reply deliveries by result.", ("result",))
        self.prompt_fetches = Counter("bridge_prompt_fetch_total", "get_prompt calls by result.", ("result",))
        self.foreign_connector = Counter("bridge_foreign_connector_total",
                                         "Calls through another member's connector that ended a live ask, by tool.",
                                         ("tool",))
        self.reply_wait = Histogram("bridge_reply_wait_seconds", "From the signed POST to a delivered reply.",
                                    WAIT_BUCKETS)
        self.queue_wait = Histogram("bridge_queue_wait_seconds", "Time an accepted /ask waited for a worker slot.",
                                    WAIT_BUCKETS)
        self.mcp_auth_failures = Counter("bridge_mcp_auth_failures_total", "Rejected /mcp bearer tokens.")
        self.inflight = Gauge("bridge_inflight", "Automation POSTs in flight.")
        self.discord_logins = Counter("bridge_discord_login_total", "Discord logins by purpose and outcome.",
                                      ("purpose", "result"))
        # source is "self" (the member, from Discord or a login) or "operator" (/v1).
        self.membership_changes = Counter("bridge_membership_changes_total",
                                          "Members joining or leaving, and automations set or removed.",
                                          ("source", "result"))

    def render(self) -> str:
        lines: list[str] = []
        for metric in (self.interactions, self.ack_latency, self.automation_posts, self.automation_latency,
                       self.replies, self.prompt_fetches, self.foreign_connector, self.reply_wait, self.queue_wait,
                       self.mcp_auth_failures, self.inflight,
                       self.discord_logins, self.membership_changes):
            lines += metric.render()
        return "\n".join(lines) + "\n"


# One registry per process. Tests replace it with reset().
METRICS = Registry()


def reset() -> Registry:
    global METRICS
    METRICS = Registry()
    return METRICS
