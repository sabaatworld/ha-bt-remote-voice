"""Bounded, anonymous capture evidence. Never stores microphone payloads."""

from collections import deque
from copy import deepcopy
from time import monotonic

FIELDS = {"count", "expected", "received", "reason", "stage", "outcome", "model", "ok"}


class CaptureHistory:
    def __init__(self):
        self.items = deque(maxlen=20)

    def begin(self, generation, *, model):
        item = {
            "generation": generation,
            "model": model,
            "events": deque(maxlen=64),
            "error": None,
            "timings": deque(maxlen=8),
            "started": monotonic(),
        }
        self.items.append(item)
        return item

    def record(self, item, kind, **values):
        item["events"].append(
            {
                "kind": kind,
                "ms": round((monotonic() - item["started"]) * 1000, 1),
                **{k: v for k, v in values.items() if k in FIELDS},
            }
        )

    def timings(self, item, values):
        item["timings"].append(
            {
                k: v
                for k, v in values.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and k.endswith("_ms")
            }
        )

    def finish(self, item, *, error=None):
        item["error"] = error
        self.record(item, "finish", reason=error)

    def snapshot(self):
        return deepcopy(
            [
                {
                    k: list(v) if k in ("events", "timings") else v
                    for k, v in item.items()
                    if k != "started"
                }
                for item in self.items
            ]
        )

    def clear(self):
        self.items.clear()
