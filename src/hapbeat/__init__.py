"""Hapbeat SDK for Python — drive Hapbeat haptic devices over Wi-Fi UDP.

Quick start::

    import hapbeat

    hb = hapbeat.connect(app_name="MyExperiment")
    hb.play("impact.hit", gain=0.3)
    hb.stop_all()
    hb.close()

The fire side (``play``/``stop``) and the tuning side (:class:`EventMap`) are
kept orthogonal and linked only by event id, matching the Hapbeat Unity SDK.
"""

from __future__ import annotations

from . import protocol
from .client import DEFAULT_BROADCAST, DEFAULT_PORT, UdpClient
from .eventmap import EventDef, EventMap
from .hapbeat import Device, Hapbeat, connect

try:  # populated from installed package metadata
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("hapbeat")
    except PackageNotFoundError:  # running from a source checkout
        __version__ = "0.1.0+local"
except ImportError:  # pragma: no cover
    __version__ = "0.1.0+local"

__all__ = [
    "connect",
    "Hapbeat",
    "Device",
    "EventMap",
    "EventDef",
    "UdpClient",
    "protocol",
    "DEFAULT_PORT",
    "DEFAULT_BROADCAST",
    "__version__",
]
