"""
Nanlite FS-300B command builders.

Feasycom "fast command" format: 8-byte payloads sent as BLE Mesh vendor
model parameters (company 0x1111, opcode 0xC1).

Reverse-engineered from the NANLINK Android app's NanCmd/FeasyCommand path.
"""

VENDOR_COMPANY_ID = 0x1111
VENDOR_OPCODE = 0xC1

# Option codes from NANLINK control configs
OPT_DIM = 0x01  # Brightness
OPT_CCT = 0x03  # Color temperature
OPT_GM = 0x04   # Green/Magenta
OPT_HUE = 0x05  # Hue
OPT_SAT = 0x0C  # Saturation


# ---------------------------------------------------------------------------
# Feasycom fast command format (BLE Mesh vendor message parameters)
# ---------------------------------------------------------------------------
# 8 bytes: [rollCode, functionCode, typeCode, optionCode, val_hi, val_lo, ch_hi, ch_lo]
#
# rollCode:     incrementing counter (anti-replay)
# functionCode: bit field - bit5=op(1=SET,0=QUERY), bit0=needReturn
# typeCode:     always 1 for light control
# optionCode:   which parameter to set (OPT_DIM, OPT_CCT, etc.)
# val:          16-bit big-endian value
# ch:           16-bit channel (0x0000 for unicast)

import time as _time

_roll_code = int(_time.time()) & 0xFF

def _next_roll() -> int:
    global _roll_code
    _roll_code = (_roll_code + 1) & 0xFF
    return _roll_code


def feasy_set(option: int, value: int) -> bytes:
    """Build a Feasycom SET command (8 bytes)."""
    func = 0x20  # bit5=1 (SET), all others 0
    return bytes([
        _next_roll(), func, 0x01, option & 0xFF,
        (value >> 8) & 0xFF, value & 0xFF,
        0x00, 0x00,
    ])


def feasy_query(option: int) -> bytes:
    """Build a Feasycom QUERY command (8 bytes)."""
    func = 0x01  # bit5=0 (QUERY), bit0=1 (needReturn)
    return bytes([
        _next_roll(), func, 0x01, option & 0xFF,
        0x00, 0x00,
        0x00, 0x00,
    ])


def _check(value: int, lo: int, hi: int, name: str) -> int:
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be {lo}–{hi}, got {value}")
    return value


def cmd_brightness(value: int) -> bytes:
    """Set brightness (0-100)."""
    return feasy_set(OPT_DIM, _check(value, 0, 100, "brightness"))


def cmd_cct(value: int) -> bytes:
    """Set color temperature (2700-6500K)."""
    return feasy_set(OPT_CCT, _check(value, 2700, 6500, "cct"))


def cmd_gm(value: int) -> bytes:
    """Set green/magenta (-50 to 50)."""
    _check(value, -50, 50, "gm")
    return feasy_set(OPT_GM, value + 50)


def cmd_hue(value: int) -> bytes:
    """Set hue (0-360)."""
    return feasy_set(OPT_HUE, _check(value, 0, 360, "hue"))


def cmd_saturation(value: int) -> bytes:
    """Set saturation (0-100)."""
    return feasy_set(OPT_SAT, _check(value, 0, 100, "saturation"))


def cmd_query() -> bytes:
    """Query device state."""
    return feasy_query(0x00)


# ---------------------------------------------------------------------------
# Extended packet: set_mode (vmedea gist, "Extended packets" / "set_mode")
# ---------------------------------------------------------------------------
# [seq, flags=0x24 (write_req|extpkt), 00 04, 01 01, opcode=01 set_mode, payload]
# payload = [ssize, brightness(2, 0-100), mode, submode, params...]
#   HSI 03:01 params = hue(2) sat(1)
#   CCT 02:02 params = 01 cct(2) gm(2, 0 = neutral) 01
# Fast commands (option 0x05/0x0C) set hue/sat but never switch the mode.

def _set_mode(brightness: int, mode: int, sub: int, params: bytes) -> bytes:
    body = brightness.to_bytes(2, "big") + bytes([mode, sub]) + params
    return bytes([_next_roll(), 0x24, 0x00, 0x04, 0x01, 0x01, 0x01, len(body) + 1]) + body


def cmd_mode_hsi(brightness: int, hue: int, sat: int) -> bytes:
    """Switch to HSI mode: brightness 0-100, hue 0-360, saturation 0-100."""
    _check(brightness, 0, 100, "brightness")
    _check(hue, 0, 360, "hue")
    _check(sat, 0, 100, "sat")
    return _set_mode(brightness, 0x03, 0x01, (hue % 360).to_bytes(2, "big") + bytes([sat]))


def cmd_mode_cct(brightness: int, cct: int, gm: int = 0) -> bytes:
    """CCT mode 02:02 with color temperature and signed G/M (0 = neutral).

    This is the only correct way to set CCT/tint on this light: the fast options
    0x03/0x04 use the FS-300B G/M encoding (50 = neutral) and push the light green
    or magenta (user-verified 2026-10-19).
    """
    _check(brightness, 0, 100, "brightness")
    _check(cct, 2700, 6500, "cct")
    _check(gm, -50, 50, "gm")
    return _set_mode(brightness, 0x02, 0x02,
                     b"\x01" + cct.to_bytes(2, "big") + gm.to_bytes(2, "big", signed=True) + b"\x01")
