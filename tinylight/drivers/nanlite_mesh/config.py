"""Shared constants and key management for Nanlite FS-300B."""

import json
import os
import time

from ...paths import data

KEYS_FILE = data("mesh_keys.json")
TEA_KEY_FILE = data("tea_key.hex")
# Feasycom TEA key from the NANLINK Android app (libencrypted.so, after "getRandomNumber").
# Published before in yuyx0407/nanlite-wand-bridge; override with FEASYCOM_TEA_KEY or tea_key.hex.
TEA_KEY_DEFAULT = "c5bae868223f9f50968717b24021c511"
DEVICE_NAME = "Nanlite (Feasycom mesh)"


def is_light(d, adv, svc: str = "1828") -> bool:
    """Match the light by mesh service (1827 = unprovisioned, 1828 = proxy).

    The Feasycom firmware advertises a junk name ("\r\nAT+MODE"), so the name is
    useless. Set NANLITE_ADDR to pin one device when several are around.
    """
    pin = os.environ.get("NANLITE_ADDR")
    if pin and d.address.upper() != pin.upper():
        return False
    return any(u.lower().startswith(f"0000{svc}") for u in (adv.service_uuids or []))

# Feasycom firmware uses the sample app key from Bluetooth Mesh Profile v1.0 §8
# (also hardcoded in their MeshManagerApi.java).
FEASYCOM_APP_KEY = "63964771734fbd76e3b40519d1d94a48"

# Epoch for time-based sequence numbers (2025-01-01T00:00:00Z).
# Using 10-second intervals: time_seq ≈ 3.5 M today, well above any saved seq
# (even after heavy usage), and exhausts the 24-bit space around 2030.
_SEQ_EPOCH = 1735689600
_SEQ_MAX = 0xFFFFFF


def load_tea_key() -> bytes:
    """Load the Feasycom TEA key (16 bytes) from env var or tea_key.hex."""
    hex_str = os.environ.get("FEASYCOM_TEA_KEY")
    if hex_str:
        return bytes.fromhex(hex_str.strip())

    try:
        with open(TEA_KEY_FILE) as f:
            return bytes.fromhex(f.read().strip())
    except FileNotFoundError:
        return bytes.fromhex(TEA_KEY_DEFAULT)


def load_keys() -> dict:
    with open(KEYS_FILE) as f:
        return json.load(f)


def device_address(keys: dict) -> str | None:
    """Return the BLE MAC address from keys, or None if not yet known."""
    return keys.get("device_address")


def save_keys(keys: dict):
    with open(KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)


def next_seq(keys: dict) -> int:
    """Return a safe starting seq that is always ahead of what the device has seen.

    Uses max(saved_seq, time_seq) + 32 where time_seq counts 10-second intervals
    since 2025-01-01.  The current time_seq (~3.5 M) is far above any realistic
    saved value, so seq is monotonically increasing across machines with stale
    keys files without coordination.  The +32 buffer covers the ~10 seq values
    consumed per connection.  The 24-bit space exhausts around 2030.
    """
    saved = keys.get("seq", 0)
    time_seq = int(time.time() - _SEQ_EPOCH) // 10
    seq = max(saved, time_seq) + 32
    if seq > _SEQ_MAX:
        raise RuntimeError(
            f"Mesh sequence space exhausted ({seq}). "
            "An IV Index update is needed (re-provision the device)."
        )
    return seq
