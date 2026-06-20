"""EventMap — the *tuning* side of the SDK, kept orthogonal to the fire side.

Mirrors the Unity SDK's EventMap concept at level-1: a catalog that maps an
event id to its per-event haptic settings (default gain, loop, mode, and -- for
clip events -- which WAV to stream). It is linked to the fire side only by
event id, so *when/where* to fire (the caller) and *what/how* to play (this
catalog) stay mutually independent.

The canonical source is the kit manifest (schema 2.0.0, see
hapbeat-contracts/specs/kit-format.md). It has two event buckets:

  - ``events``        — **command** mode: the device plays a clip already
    installed on it; the SDK just sends a PLAY with the event id.
  - ``stream_events`` — **clip** mode: the SDK reads the event's WAV from the
    kit's ``stream-clips/`` folder and UDP-streams it to the device.

``play(event_id)`` branches on which bucket the event came from, so the caller
writes the same one-liner either way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


@dataclass
class EventDef:
    """Per-event haptic settings resolved from a kit manifest (or by hand)."""

    event_id: str
    intensity: float = 1.0
    loop: bool = False
    device_wiper: Optional[int] = None
    streaming: bool = False  # True => clip mode (manifest stream_events bucket)
    clip: str = ""           # WAV filename (clip mode), relative to stream-clips/
    target: str = ""         # device address (overlay/app side); "" = broadcast
    note: str = ""

    @property
    def mode(self) -> str:
        """``"clip"`` for stream events, ``"command"`` otherwise."""
        return "clip" if self.streaming else "command"


class EventMap:
    """A catalog of event definitions keyed by event id.

    ``kit_dir`` (when known) is the folder that holds the manifest; clip-mode
    WAVs are resolved relative to ``<kit_dir>/stream-clips/``.
    """

    def __init__(
        self,
        events: Optional[dict[str, EventDef]] = None,
        *,
        kit_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self._events: dict[str, EventDef] = dict(events or {})
        self.kit_dir: Optional[Path] = Path(kit_dir) if kit_dir else None

    # ── Construction ────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, gains: dict[str, float]) -> "EventMap":
        """Build from a simple ``{event_id: gain}`` mapping (all command mode)."""
        return cls({k: EventDef(event_id=k, intensity=float(v)) for k, v in gains.items()})

    @classmethod
    def from_manifest(
        cls,
        manifest: Union[str, Path, dict],
        *,
        kit_dir: Optional[Union[str, Path]] = None,
    ) -> "EventMap":
        """Build from a kit manifest (path, JSON string, or parsed dict).

        Reads schema 2.0.0 ``events`` (command) and ``stream_events`` (clip)
        buckets. When ``manifest`` is a file path and ``kit_dir`` is not given,
        the manifest's parent folder becomes the kit dir (so clip WAVs resolve).
        """
        src_dir: Optional[Path] = Path(kit_dir) if kit_dir else None
        if isinstance(manifest, (str, Path)) and not _looks_like_json(manifest):
            path = Path(manifest)
            if src_dir is None:
                src_dir = path.parent
            manifest = json.loads(path.read_text(encoding="utf-8"))
        elif isinstance(manifest, str):
            manifest = json.loads(manifest)

        events: dict[str, EventDef] = {}
        # events first, then stream_events: for an id authored in BOTH buckets
        # (Studio "BOTH" mode) the clip variant wins, matching the web SDK.
        for bucket, streaming in (("events", False), ("stream_events", True)):
            for event_id, entry in (manifest.get(bucket) or {}).items():
                entry = entry or {}
                params = entry.get("parameters") or {}
                events[event_id] = EventDef(
                    event_id=event_id,
                    intensity=float(params.get("intensity", 1.0)),
                    loop=bool(params.get("loop", False)),
                    device_wiper=params.get("device_wiper"),
                    streaming=streaming,
                    clip=entry.get("clip", ""),
                    note=entry.get("note", ""),
                )
        return cls(events, kit_dir=src_dir)

    @classmethod
    def from_kit(cls, kit_dir: Union[str, Path]) -> "EventMap":
        """Build from a kit folder, discovering ``*-manifest.json`` inside it.

        The recommended project layout is::

            kits/<kit-name>/
                <kit-name>-manifest.json
                install-clips/   (command clips, deployed to the device)
                stream-clips/    (clip-mode WAVs the SDK streams)
        """
        d = Path(kit_dir)
        candidates = sorted(d.glob("*-manifest.json")) or sorted(d.glob("manifest.json"))
        if not candidates:
            raise FileNotFoundError(f"no *-manifest.json found in kit folder {d}")
        return cls.from_manifest(candidates[0], kit_dir=d)

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "EventMap":
        """Build from a *haptic file* — an authored overlay that references a kit.

        The Studio-generated manifest holds kit content (intensity / clip /
        mode). App-side, per-event settings that the manifest does NOT carry --
        targeting, a gain override -- live here, so ``play(id)`` resolves them
        without the caller passing ``target`` every time. Mirrors the Unity
        SDK's EventMap asset (which references the kit and adds target/gain).

        File format (JSON)::

            {
              "kit": "kits/my-kit",          # kit folder, relative to this file
              "events": {
                "impact.hit": { "target": "player_1/chest", "gain": 0.8 },
                "rain.loop":  { "target": "*/back" }
              }
            }

        ``kit`` is optional (omit it for a pure target/gain overlay with no
        clip resolution). Per-event keys: ``target``, ``gain`` (overrides the
        manifest intensity; ``intensity`` is also accepted), ``loop``, ``note``.
        """
        p = Path(path)
        spec = json.loads(p.read_text(encoding="utf-8"))
        kit = spec.get("kit")
        if kit:
            kit_path = Path(kit)
            if not kit_path.is_absolute():
                kit_path = p.parent / kit_path
            em = cls.from_kit(kit_path)
        else:
            em = cls()

        for event_id, raw in (spec.get("events") or {}).items():
            o = raw or {}
            ev = em._events.get(event_id)
            if ev is None:  # overlay-only event (not in the kit): command mode
                ev = EventDef(event_id=event_id)
                em._events[event_id] = ev
            if "target" in o:
                ev.target = str(o["target"])
            if "gain" in o:
                ev.intensity = float(o["gain"])
            elif "intensity" in o:
                ev.intensity = float(o["intensity"])
            if "loop" in o:
                ev.loop = bool(o["loop"])
            if "note" in o:
                ev.note = str(o["note"])
        return em

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
