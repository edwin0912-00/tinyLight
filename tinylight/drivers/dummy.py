"""A virtual light: no Bluetooth, prints what a real driver would send. For tests and demos."""

from __future__ import annotations

from .base import Driver


class DummyDriver(Driver):
    name = "dummy"
    title = "Virtual light (no hardware)"

    def __init__(self):
        self._up = False
        self.sent: list[dict] = []

    def connected(self) -> bool:
        return self._up

    async def connect(self):
        self._up = True

    async def disconnect(self):
        self._up = False

    async def set(self, changes: dict, state: dict) -> dict:
        out = dict(changes)
        lo, hi = self.capabilities["cct"]
        if "cct" in out and not lo <= out["cct"] <= hi:
            raise ValueError(f"cct must be {lo}-{hi}")
        if "hue" in out or "sat" in out:
            out["mode"] = "hsi"
        elif "cct" in out or "gm" in out:
            out["mode"] = "cct"
        self.sent.append(out)
        print(f"dummy light <- {out}", flush=True)
        return out

    async def raw(self, payload: bytes):
        print(f"dummy light <- raw {payload.hex()}", flush=True)
