"""Nanlite lights on the Feasycom Bluetooth-mesh SoC (verified on a room light, 2026-10).

Fingerprint: advertises Mesh Provisioning 0x1827 (unpaired) or Mesh Proxy 0x1828 (paired); GATT also has
Feasycom 0xFFF0 (TEA auth) and Airoha DFU. Advertised name is junk ("\\r\\nAT+MODE").
Control: vendor model C1 11 11. Brightness = fast option 0x01. CCT + tint and color go ONLY through the
extended set_mode packet (02:02 CCT with signed G/M, 03:01 HSI); the fast CCT/GM options use another
model's G/M encoding and tint the light. Protocol notes: docs/nanlite-mesh.md.
"""

from __future__ import annotations

from bleak import BleakScanner

from ..base import Driver
from .config import is_light, load_keys, next_seq, save_keys
from .mesh_proxy import MeshProxyClient
from .protocol import VENDOR_COMPANY_ID, VENDOR_OPCODE, cmd_brightness, cmd_mode_cct, cmd_mode_hsi


class NanliteMeshDriver(Driver):
    name = "nanlite_mesh"
    title = "Nanlite (Feasycom Bluetooth mesh)"
    capabilities = {"cct": (2700, 6500), "gm": (-50, 50), "hsi": True}

    def __init__(self):
        self.keys = load_keys()
        self.client: MeshProxyClient | None = None

    @classmethod
    def match(cls, name, services, manufacturer) -> float:
        s = {u.lower()[4:8] if len(u) > 8 else u.lower() for u in services}
        score = 0.0
        if {"1827", "1828"} & s:
            score += 0.5                      # any Bluetooth-mesh node
        if "fff0" in s:
            score += 0.3                      # Feasycom auth service (seen in GATT)
        if name and "AT+MODE" in name:
            score += 0.2                      # Feasycom firmware leaks an AT command as its name
        return min(score, 1.0)

    def is_target(self, device, adv) -> bool:
        return is_light(device, adv)

    def connected(self) -> bool:
        return bool(self.client and self.client._client and self.client._client.is_connected)

    async def connect(self):
        dev = await BleakScanner.find_device_by_filter(lambda d, adv: is_light(d, adv), timeout=15)
        if dev is None:
            raise RuntimeError("light not found (off, out of range, or held by another controller)")
        k = self.keys
        c = MeshProxyClient(bytes.fromhex(k["net_key"]), bytes.fromhex(k["app_key"]),
                            bytes.fromhex(k["dev_key"]), k["iv_index"], src_addr=0x0001)
        c.crypto._seq = next_seq(k)
        await c.connect(dev)
        self.client = c

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None

    async def _send(self, payloads: list[bytes]):
        import asyncio
        for p in payloads:
            await self.client.send_vendor_message(self.keys["unicast_address"], VENDOR_OPCODE, VENDOR_COMPANY_ID, p)
            await asyncio.sleep(0.01)
        self.keys["seq"] = self.client.crypto._seq
        save_keys(self.keys)

    async def set(self, changes: dict, state: dict) -> dict:
        values = dict(changes)
        b = values.get("brightness", state.get("last_on_brightness", 50) if state.get("on") else 0)
        if "hue" in values or "sat" in values:
            hue, sat = values.get("hue", state.get("hue", 0)), values.get("sat", state.get("sat", 100))
            payloads = [cmd_mode_hsi(b, hue, sat)]
            values.update(hue=hue, sat=sat, mode="hsi")
        elif "cct" in values or "gm" in values:
            values.setdefault("cct", state.get("cct", 4000))
            values.setdefault("gm", state.get("gm", 0))
            payloads = [cmd_mode_cct(b, values["cct"], values["gm"])]
            values["mode"] = "cct"
        elif "brightness" in values:
            payloads = [cmd_brightness(b)]
        else:
            payloads = []
        await self._send(payloads)
        return values

    async def raw(self, payload: bytes):
        await self._send([payload])

    @classmethod
    async def pair(cls, log=print) -> dict:
        """Provision an unpaired light (advertising 0x1827) with fresh keys, then bind the app key."""
        from . import provision
        await provision.main()
        return load_keys()
