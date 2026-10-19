"""Template for a new light family. Copy with `tinylight new-driver <name>`, then fill the TODOs.

Workflow that found the Nanlite protocol (docs/connect-any-light.md):
  1. `tinylight probe <address>` -> GATT table (services, characteristics, properties) in probes/.
  2. Search GitHub for prior work: the service UUIDs, the advertised name, the vendor app id.
  3. No prior work: read the vendor's Android app (e.g. REA / JADX on the APK) for the packet builder,
     or capture traffic (Android HCI snoop log, macOS PacketLogger) while you move one slider at a time.
  4. Implement set() for brightness first, test with `tinylight try <driver> brightness 10`, then
     CCT, then color. Keep every finding in docs/<driver>.md.
"""

from __future__ import annotations

from bleak import BleakClient, BleakScanner

from .base import Driver

# TODO: the light's GATT write characteristic and its advertised service UUID
SERVICE = "0000ffe0-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"


class TemplateDriver(Driver):
    name = "template"
    title = "TODO: vendor + model family"
    capabilities = {"cct": (2700, 6500), "gm": (-50, 50), "hsi": True}

    def __init__(self, address: str | None = None):
        self.address = address          # TODO: store it in config.json during `tinylight connect`
        self.client: BleakClient | None = None

    @classmethod
    def match(cls, name, services, manufacturer) -> float:
        return 0.9 if SERVICE in services else 0.0      # TODO: a precise fingerprint

    def is_target(self, device, adv) -> bool:
        return SERVICE in (adv.service_uuids or [])

    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def connect(self):
        dev = await BleakScanner.find_device_by_filter(self.is_target, timeout=15)
        if dev is None:
            raise RuntimeError("light not found")
        self.client = BleakClient(dev)
        await self.client.connect()
        # TODO: authentication / handshake if the light needs one

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()
        self.client = None

    async def set(self, changes: dict, state: dict) -> dict:
        values = dict(changes)
        if "brightness" in values:
            level = int(values["brightness"])
            packet = bytes([0x01, level])          # TODO: real packet layout
            await self.client.write_gatt_char(WRITE_CHAR, packet, response=False)
        # TODO: cct/gm (white) and hue/sat (color); return mode="cct" or "hsi" when it changes
        return values

    async def raw(self, payload: bytes):
        await self.client.write_gatt_char(WRITE_CHAR, payload, response=False)
