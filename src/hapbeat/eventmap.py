"""EventMap — the *tuning* side of the SDK, kept orthogonal to the fire side.

Mirrors the Unity SDK's EventMap concept at level-1: a catalog that maps an
event id to its default gain (and other per-event metadata). It is linked to
the fire side only by event id, so triggers and tuning stay mutually
independent.

The canonical source of per-event default intensity is the kit manifest
(schema 2.0.0, see hapbeat-contracts/specs/kit-format.md). ``intensity`` in the
manifest is the recommended baseline gain; the SDK reads it so that
``hb.play("event.id")`` (no explicit gain) fires at the authored strength.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


@dataclass
class EventDef:
    """Per-event tuning resolved from a kit manifest (or set by hand)."""

    event_id: str
    intensity: float = 1.0
    loop: bool = False
    device_wiper: Optional[int] = None
    streaming: bool = False  # True if it came from the manifest stream_events bucket
    note: str = ""


class EventMap:
    """A catalog of event definitions keyed by event id."""

    def __init__(self, events: Optional[dict[str, EventDef]] = None) -> None:
        self._events: dict[str, EventDef] = dict(events or {})

    # ── Construction ────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, gains: dict[str, float]) -> "EventMap":
        """Build from a simple ``{event_id: gain}`` mapping."""
        return cls({k: EventDef(event_id=k, intensity=float(v)) for k, v in gains.items()})

    @classmethod
    def from_manifest(cls, manifest: Union[str, Path, dict]) -> "EventMap":
        """Build from a kit manifest (path, JSON string, or parsed dict).

        Reads schema 2.0.0 ``events`` (command) and ``stream_events`` buckets.
        """
        if isinstance(manifest, (str, Path)) and not _looks_like_json(manifest):
            manifest = json.loads(Path(manifest).read_text(encoding="utf-8"))
        elif isinstance(manifest, str):
            manifest = json.loads(manifest)

        events: dict[str, EventDef] = {}
        for bucket, streaming in (("events", False), ("stream_events", True)):
            for event_id, entry in (manifest.get(bucket) or {}).items():
                params = (entry or {}).get("parameters") or {}
                events[event_id] = EventDef(
                    event_id=event_id,
                    intensity=float(params.get("intensity", 1.0)),
                    loop=bool(params.get("loop", False)),
                    device_wiper=params.get("device_wiper"),
                    streaming=streaming,
                    note=(entry or {}).get("note", ""),
                )
        return cls(events)

    # ── Lookup ──────────────────────────────────────────────────────
    def gain_for(self, event_id: str) -> float:
        """Default gain for an event (its manifest intensity), or 1.0."""
        ev = self._events.get(event_id)
        return ev.intensity if ev is not None else 1.0

    def get(self, event_id: str) -> Optional[EventDef]:
        return self._events.get(event_id)

    def add(self, event_id: str, intensity: float = 1.0, **kw) -> None:
        self._events[event_id] = EventDef(event_id=event_id, intensity=intensity, **kw)

    def ids(self) -> list[str]:
        return list(self._events.keys())

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._events

    def __len__(self) -> int:
        return len(self._events)


def _looks_like_json(s: Union[str, Path]) -> bool:
    return isinstance(s, str) and s.lstrip().startswith("{")
