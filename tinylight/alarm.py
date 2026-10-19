"""Sunrise alarms: light and sound curves relative to the alarm time T (minutes, negative = before).

alarms.json:
  {"alarms": [{"id": "a1", "time": "07:30", "days": [0,1,2,3,4], "enabled": true,
               "mode": "gentle", "sound": "birds-spain"}],
   "modes": {"gentle": {"name": ..., "light": [points], "sound": [points], "auto_off": 90}}}
days: 0 = Monday. Light point: {"t", "level", "kelvin" | "hue"+"sat", "ease_in", "ease_out", "hold"}.
Sound point: {"t", "volume" 0-100, "sound"? (switch file from here), eases}. Same AE-style bezier
easing as the daily schedule. Morning only: alarm 04:00-11:59, the light never starts before 03:30.
Design source: reports/Світанковий будильник для лампи.md (30 min dawn ending at T, sound from -3).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from . import paths
from .schedule import DEFAULT_EASE, bezier

HERE = os.path.dirname(os.path.abspath(__file__))
ALARMS_FILE = paths.data("alarms.json")
SOUNDS_DIR = paths.SOUNDS
EARLIEST_ALARM, LATEST_ALARM, EARLIEST_LIGHT = 4 * 60, 11 * 60 + 59, 3 * 60 + 30
SNOOZE_MIN = 9
RING_MINUTES = 5  # auto-stop: the alarm stops by itself this long after it starts ringing

AMBER = {"hue": 25, "sat": 100}
MODES = {
    "gentle": {"name": "Gentle sunrise", "auto_off": 90,
               "light": [{"t": -30, "level": 1, **AMBER}, {"t": -20, "level": 4, "hue": 32, "sat": 80},
                         {"t": -10, "level": 22, "kelvin": 2700}, {"t": 0, "level": 70, "kelvin": 4000},
                         {"t": 60, "level": 70, "kelvin": 4000, "hold": True}],
               "sound": [{"t": -3, "volume": 0}, {"t": 0, "volume": 40}, {"t": 3, "volume": 60},
                         {"t": 60, "volume": 60, "hold": True}]},
    "long": {"name": "Long winter dawn", "auto_off": 90,
             "light": [{"t": -60, "level": 1, **AMBER}, {"t": -40, "level": 5, "hue": 32, "sat": 80},
                       {"t": -20, "level": 25, "kelvin": 2700}, {"t": 0, "level": 75, "kelvin": 4200},
                       {"t": 60, "level": 75, "kelvin": 4200, "hold": True}],
             "sound": [{"t": -5, "volume": 0}, {"t": 0, "volume": 45}, {"t": 3, "volume": 60},
                       {"t": 60, "volume": 60, "hold": True}]},
    "quick": {"name": "Quick", "auto_off": 60,
              "light": [{"t": -15, "level": 1, "kelvin": 2700}, {"t": -7, "level": 15, "kelvin": 2700},
                        {"t": 0, "level": 65, "kelvin": 4000}, {"t": 60, "level": 65, "kelvin": 4000, "hold": True}],
              "sound": [{"t": 0, "volume": 0, "ease_out": 0}, {"t": 0.5, "volume": 60, "ease_in": 0},
                        {"t": 60, "volume": 60, "hold": True}]},
    "light": {"name": "Light only", "auto_off": 35,
              "light": [{"t": -30, "level": 1, **AMBER}, {"t": -15, "level": 15, "kelvin": 2700},
                        {"t": 0, "level": 55, "kelvin": 3500}, {"t": 30, "level": 55, "kelvin": 3500},
                        {"t": 35, "level": 20, "kelvin": 3000}],
              "sound": []},
    "backup": {"name": "With backup", "auto_off": 90,
               "light": [{"t": -30, "level": 1, **AMBER}, {"t": -20, "level": 4, "hue": 32, "sat": 80},
                         {"t": -10, "level": 22, "kelvin": 2700}, {"t": 0, "level": 70, "kelvin": 4000},
                         {"t": 10, "level": 70, "kelvin": 4000, "ease_out": 0},
                         {"t": 11, "level": 100, "kelvin": 5000, "ease_in": 0},
                         {"t": 60, "level": 100, "kelvin": 5000, "hold": True}],
               "sound": [{"t": -3, "volume": 0}, {"t": 0, "volume": 40}, {"t": 3, "volume": 55},
                         {"t": 10, "volume": 55, "ease_out": 0},
                         {"t": 10.5, "volume": 80, "sound": "wind-chimes", "ease_in": 0},
                         {"t": 60, "volume": 80, "hold": True}]},
}
DEFAULT = {"alarms": [], "modes": MODES}


def load() -> dict:
    try:
        with open(ALARMS_FILE) as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        d = json.loads(json.dumps(DEFAULT))
    d.setdefault("ring_minutes", RING_MINUTES)
    for k, v in MODES.items():  # new built-in modes appear without wiping user edits
        d.setdefault("modes", {}).setdefault(k, json.loads(json.dumps(v)))
    return d


def save(d: dict):
    validate(d)
    tmp = ALARMS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ALARMS_FILE)


def sounds() -> list[str]:
    try:
        return sorted(f[:-4] for f in os.listdir(SOUNDS_DIR) if f.endswith(".m4a"))
    except FileNotFoundError:
        return []


def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {hhmm}")
    return h * 60 + m


def validate(d: dict):
    modes = d.get("modes", {})
    if not 1 <= float(d.get("ring_minutes", RING_MINUTES)) <= 60:
        raise ValueError("ring_minutes: 1-60")
    for a in d.get("alarms", []):
        m = _minutes(a["time"])
        if not EARLIEST_ALARM <= m <= LATEST_ALARM:
            raise ValueError("alarms are morning only: 04:00-11:59")
        if a.get("mode") not in modes:
            raise ValueError(f"unknown mode {a.get('mode')}")
        if not set(a.get("days", [])) <= set(range(7)):
            raise ValueError("days: 0 (Mon) .. 6 (Sun)")
        if a.get("sound") and a["sound"] not in sounds():
            raise ValueError(f"unknown sound {a['sound']}; have {', '.join(sounds())}")
        start = m + min((p["t"] for p in modes[a["mode"]]["light"]), default=0)
        if start < EARLIEST_LIGHT:
            raise ValueError("this mode would start the light before 03:30")
    for k, mode in modes.items():
        for p in mode.get("light", []):
            if not 0 <= p.get("level", -1) <= 100:
                raise ValueError(f"{k}: light level 0-100")
            if "hue" not in p and not 2700 <= p.get("kelvin", 0) <= 6500:
                raise ValueError(f"{k}: light point needs kelvin 2700-6500 or hue")
        for p in mode.get("sound", []):
            if not 0 <= p.get("volume", -1) <= 100:
                raise ValueError(f"{k}: sound volume 0-100")


def occurrence(a: dict, mode: dict, now: datetime) -> datetime | None:
    """The alarm time T whose window [first point, T + auto_off] contains or follows now."""
    if not a.get("enabled", True):
        return None
    m = _minutes(a["time"])
    days = set(a.get("days", range(7)))
    first = min((p["t"] for p in mode["light"] + mode["sound"]), default=0)
    for d in range(-1, 8):
        day = (now + timedelta(days=d)).date()
        if day.weekday() not in days:
            continue
        t = datetime(day.year, day.month, day.day, m // 60, m % 60).astimezone()
        if t + timedelta(minutes=mode.get("auto_off", 90)) > now:
            return t
    return None


def window(mode: dict, t: datetime) -> tuple[datetime, datetime]:
    first = min((p["t"] for p in mode["light"] + mode["sound"]), default=0)
    return t + timedelta(minutes=first), t + timedelta(minutes=mode.get("auto_off", 90))


def _segment(pts: list[dict], x: float):
    pts = sorted(pts, key=lambda p: p["t"])
    if x <= pts[0]["t"]:
        return pts[0], pts[0], 0.0
    for a, b in zip(pts, pts[1:]):
        if a["t"] <= x < b["t"]:
            p = (x - a["t"]) / (b["t"] - a["t"])
            q = 0.0 if a.get("hold") else bezier(
                p, a.get("ease_out", DEFAULT_EASE) / 100, 1 - b.get("ease_in", DEFAULT_EASE) / 100)
            return a, b, q
    return pts[-1], pts[-1], 0.0


def light_at(mode: dict, x: float) -> dict | None:
    """Light target x minutes from T: {level, kelvin} or {level, hue, sat}."""
    pts = mode.get("light") or []
    if not pts or x < min(p["t"] for p in pts):
        return None
    a, b, q = _segment(pts, x)
    level = a["level"] + (b["level"] - a["level"]) * q
    if "hue" in a:
        # Toward a white point: drift to warm orange and drop saturation, then switch at b.
        bh, bs = (b["hue"], b["sat"]) if "hue" in b else (35, 30)
        return {"level": level, "hue": a["hue"] + (bh - a["hue"]) * q, "sat": a["sat"] + (bs - a["sat"]) * q}
    kb = b.get("kelvin", a["kelvin"])
    return {"level": level, "kelvin": a["kelvin"] + (kb - a["kelvin"]) * q}


def sound_at(mode: dict, x: float, default_sound: str | None) -> dict | None:
    """Sound target x minutes from T: {sound, volume} or None before the first sound point."""
    pts = mode.get("sound") or []
    if not pts or x < min(p["t"] for p in pts):
        return None
    a, b, q = _segment(pts, x)
    current = default_sound
    for p in sorted(pts, key=lambda p: p["t"]):
        if p["t"] <= x and p.get("sound"):
            current = p["sound"]
    return {"sound": current, "volume": a["volume"] + (b["volume"] - a["volume"]) * q}
