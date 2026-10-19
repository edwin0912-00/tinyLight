"""The one interface every light driver implements. The bridge, schedule, alarms, MCP, HomeKit and
the menu bar app only talk to this, so a new light needs only a new driver.

Light state the bridge keeps (and passes to set()):
    brightness 0-100, on, mode "cct" | "hsi", cct (K), gm (-50..50, 0 neutral), hue 0-360, sat 0-100
"""

from __future__ import annotations


class Driver:
    #: short id used in config.json ("driver": ...)
    name = "base"
    #: human name of the light family
    title = "Base driver"
    #: what the light can do; the UI and API clamp to these
    capabilities = {"cct": (2700, 6500), "gm": (-50, 50), "hsi": True}

    # --- discovery -----------------------------------------------------------------------------

    @classmethod
    def match(cls, name: str | None, services: set[str], manufacturer: dict[int, bytes]) -> float:
        """0..1: how sure we are an advertising device is this light family.

        services: lower-case 4-char (16-bit) or full UUID strings from the advertisement/GATT.
        """
        return 0.0

    def is_target(self, device, adv) -> bool:
        """True for the paired light among scan results (bleak BLEDevice + AdvertisementData)."""
        return False

    # --- link ----------------------------------------------------------------------------------

    def connected(self) -> bool:
        raise NotImplementedError

    async def connect(self):
        """Find and connect to the paired light. Raise RuntimeError with a plain message on failure."""
        raise NotImplementedError

    async def disconnect(self):
        pass

    # --- control -------------------------------------------------------------------------------

    async def set(self, changes: dict, state: dict) -> dict:
        """Send `changes` (any of brightness, cct, gm, hue, sat) given the current `state`.

        Return the values that are now true (e.g. add mode="hsi" when switching to color).
        Brightness 0 means off. Validate ranges and raise ValueError for bad input.
        """
        raise NotImplementedError

    async def raw(self, payload: bytes):
        """Optional: send a raw protocol payload (protocol probes, localhost only)."""
        raise NotImplementedError

    # --- setup ---------------------------------------------------------------------------------

    @classmethod
    async def pair(cls, log=print) -> dict:
        """Optional: first-time pairing/provisioning; return what to save (keys etc.)."""
        raise NotImplementedError
