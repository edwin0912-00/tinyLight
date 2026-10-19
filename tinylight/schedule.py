"""Daily light schedule: a 24 h curve of keyframes with easing, points can follow sunset.

schedule.json:
  {"enabled": true, "lat": 39.47, "lon": -0.376,
   "points": [{"time": "06:00", "level": 0, "kelvin": 4000, "ease_in": 50, "ease_out": 60},
              {"sun": "sunset", "offset": -60, "level": 50, "kelvin": 5000}, ...]}
level 0-100 (0 = off), kelvin 2700-6500. Easing works like After Effects keyframes:
ease_out = influence (0-100 %) leaving the point, ease_in = influence arriving at it.
A segment A -> B is the cubic bezier (A.ease_out, 0)-(1 - B.ease_in, 1): 0/0 is linear,
higher values flatten the curve at the point (soft start/stop). hold: true keeps the
point's value until the next point. Times are local (the bridge host clock).
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
from . import paths

SCHEDULE_FILE = paths.data("schedule.json")
DEFAULT_EASE = 50
# Old "ease" names -> (ease_out of this point, ease_in of the next point).
LEGACY = {"linear": (0, 0), "smooth": (50, 50), "in": (66, 0), "out": (0, 66)}


def bezier(p: float, x1: float, x2: float) -> float:
    """y for x=p on the cubic bezier (0,0)-(x1,0)-(x2,1)-(1,1); x1, x2 in [0, 1]."""
    lo, hi = 0.0, 1.0
    for _ in range(40):  # bisection: x(t) is monotonic for x1, x2 in [0, 1]
        t = (lo + hi) / 2
        x = 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t * t * x2 + t ** 3
        lo, hi = (t, hi) if x < p else (lo, t)
    t = (lo + hi) / 2
    return 3 * (1 - t) * t * t + t ** 3


def migrate(s: dict) -> dict:
    """Convert old per-segment "ease" names to per-point ease_in/ease_out/hold."""
    pts = s.get("points", [])
    if not any("ease" in p for p in pts):
        return s
    n = len(pts)
    for i, p in enumerate(pts):
        e = p.pop("ease", "smooth")
        if e == "hold":
            p["hold"] = True
            continue
        out_, in_ = LEGACY.get(e, (50, 50))
        p.setdefault("ease_out", out_)
        pts[(i + 1) % n].setdefault("ease_in", in_)
    return s

DEFAULT = {
    "enabled": False,
    # Location for sunrise/sunset points: config.json "lat"/"lon" (tinylight connect asks for it).
    "lat": paths.config().get("lat", 51.48), "lon": paths.config().get("lon", 0.0),
    "points": [
        {"time": "01:00", "level": 1, "kelvin": 2700, "ease": "hold"},
        {"time": "03:00", "level": 0, "kelvin": 2700, "ease": "hold"},
        {"time": "06:00", "level": 0, "kelvin": 4000, "ease": "smooth"},
        {"time": "08:00", "level": 50, "kelvin": 5000, "ease": "hold"},
        {"sun": "sunset", "offset": -60, "level": 50, "kelvin": 5000, "ease": "smooth"},
        {"sun": "sunset", "offset": 20, "level": 58, "kelvin": 3200, "ease": "smooth"},
        {"time": "21:30", "level": 65, "kelvin": 2700, "ease": "smooth"},
        {"time": "23:30", "level": 12, "kelvin": 2700, "ease": "out"},
    ],
}


def load() -> dict:
    try:
        with open(SCHEDULE_FILE) as f:
            return migrate(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return migrate(json.loads(json.dumps(DEFAULT)))


def save(s: dict):
    validate(s)
    tmp = SCHEDULE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SCHEDULE_FILE)


def validate(s: dict):
    pts = s.get("points")
    if not isinstance(pts, list) or len(pts) < 2:
        raise ValueError("points: need at least 2")
    for p in pts:
        if "time" in p:
            h, m = map(int, str(p["time"]).split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError(f"bad time {p['time']}")
        elif p.get("sun") not in ("sunset", "sunrise"):
            raise ValueError("point needs time HH:MM or sun sunset/sunrise")
        if not 0 <= int(p.get("level", -1)) <= 100:
            raise ValueError("level must be 0-100")
        if not 2700 <= int(p.get("kelvin", 0)) <= 6500:
            raise ValueError("kelvin must be 2700-6500")
        for k in ("ease_in", "ease_out"):
            if not 0 <= float(p.get(k, DEFAULT_EASE)) <= 100:
                raise ValueError(f"{k} must be 0-100")


def sun_minutes(day: date, lat: float, lon: float, event: str = "sunset") -> float:
    """Local clock minutes of sunrise/sunset (Almanac for Computers, ~1 min accuracy)."""
    n = day.timetuple().tm_yday
    lng_hour = lon / 15
    t = n + ((18 if event == "sunset" else 6) - lng_hour) / 24
    m = 0.9856 * t - 3.289
    r = math.radians
    l = (m + 1.916 * math.sin(r(m)) + 0.020 * math.sin(r(2 * m)) + 282.634) % 360
    ra = math.degrees(math.atan(0.91764 * math.tan(r(l)))) % 360
    ra = (ra + (l // 90) * 90 - (ra // 90) * 90) / 15
    sin_dec = 0.39782 * math.sin(r(l))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (math.cos(r(90.833)) - sin_dec * math.sin(r(lat))) / (cos_dec * math.cos(r(lat)))
    h = math.degrees(math.acos(max(-1, min(1, cos_h))))
    h = (h if event == "sunset" else 360 - h) / 15
    ut = (h + ra - 0.06571 * t - 6.622 - lng_hour) % 24
    noon = datetime(day.year, day.month, day.day, 12).astimezone()
    offset_h = noon.utcoffset().total_seconds() / 3600
    return ((ut + offset_h) % 24) * 60


def resolved(s: dict, day: date) -> list[dict]:
    """Points with absolute minute-of-day `m`, sorted."""
    out = []
    for p in s["points"]:
        if "time" in p:
            h, mm = map(int, p["time"].split(":"))
            m = h * 60 + mm
        else:
            m = sun_minutes(day, s["lat"], s["lon"], p["sun"]) + p.get("offset", 0)
        out.append({**p, "m": m % 1440})
    return sorted(out, key=lambda p: p["m"])


def value_at(s: dict, now: datetime | None = None) -> dict:
    """Target {level, kelvin} for a moment, plus the next keyframe as epoch seconds."""
    now = now or datetime.now().astimezone()
    pts = resolved(s, now.date())
    m = now.hour * 60 + now.minute + now.second / 60
    prev = [p for p in pts if p["m"] <= m]
    a = prev[-1] if prev else pts[-1]
    ai = pts.index(a)
    b = pts[(ai + 1) % len(pts)]
    ta = a["m"] if a["m"] <= m else a["m"] - 1440
    tb = b["m"] if b["m"] > ta else b["m"] + 1440
    p = (m - ta) / (tb - ta) if tb > ta else 0.0
    p = max(0.0, min(1.0, p))
    q = 0.0 if a.get("hold") else bezier(
        p, a.get("ease_out", DEFAULT_EASE) / 100, 1 - b.get("ease_in", DEFAULT_EASE) / 100)
    nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=tb - math.floor(m))
    return {"level": a["level"] + (b["level"] - a["level"]) * q,
            "kelvin": a["kelvin"] + (b["kelvin"] - a["kelvin"]) * q,
            "next_point": nxt.timestamp()}


def preview(s: dict, step: int = 10) -> list[dict]:
    """Today's curve every `step` minutes (for editors and agents)."""
    day = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(0, 1440, step):
        v = value_at(s, day + timedelta(minutes=i))
        out.append({"minute": i, "level": round(v["level"], 1), "kelvin": round(v["kelvin"])})
    return out


def info(s: dict) -> dict:
    today = datetime.now().astimezone().date()
    return {**s, "today": {
        "sunset": _hhmm(sun_minutes(today, s["lat"], s["lon"], "sunset")),
        "sunrise": _hhmm(sun_minutes(today, s["lat"], s["lon"], "sunrise")),
        "points": [{**p, "at": _hhmm(p["m"])} for p in resolved(s, today)],
        "now": {k: round(v) for k, v in value_at(s).items() if k != "next_point"},
    }}


def _hhmm(m: float) -> str:
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


if __name__ == "__main__":  # quick self-check
    s = load()
    print(json.dumps(info(s)["today"], indent=1))
    print(time.strftime("%F %T %Z"))
