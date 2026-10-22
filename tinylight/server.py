"""tinyLight bridge - the one owner of the Bluetooth link to a light, HTTP API for everyone else.

The light itself is a driver (tinylight/drivers, chosen in $TINYLIGHT_HOME/config.json). The bridge keeps
the link warm so commands land in ~0.1 s, and runs scenes, the daily schedule and sunrise alarms.
Clients: menu bar app, MCP connector (stdio or POST /mcp), Apple Home accessory, CLI, curl, Shortcuts.

  GET /state                         -> last values sent (the light has no readback)
  GET /on | /off                     -> off = brightness 0, on = last non-zero brightness
  GET /set?brightness=0-100&cct=2700-6500&gm=-50..50
  GET /set?hue=0-360&sat=0-100[&brightness=]   -> HSI color mode
      (cct or gm after a color switches the light back to CCT mode)
  GET /raw?hex=<driver payload>               -> localhost only, for protocol probes
  GET /events                                 -> server-sent events: state, alarm, calibrate
  More: /scenes /schedule /alarms /alarm/* /calibrate* /devices /time /sounds/<name>.m4a, POST /mcp

Listens on TINYLIGHT_HOST:TINYLIGHT_PORT (default 0.0.0.0:8787). Requests from other
hosts need ?token=<contents of token file> or header X-Token.
"""

import asyncio
import json
import os
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

from . import paths
from . import schedule as sched
from . import alarm as alarms
from .drivers import load as load_driver
import queue
import threading
from datetime import datetime

# Push hub: every client keeps one /events stream; stop/snooze/state reach all devices at once.
SUBS: list = []
ONLINE: dict = {}  # device name -> number of open /events streams
STATUS: dict = {}  # device name -> last self-reported status (Bluetooth role/state, clock offset)


def analyse_calibration(body: dict) -> dict:
    """Arrival time and level of each device's clicks in the recorder's envelope (server clock)."""
    env = body["env"]                      # [[server_time, peak], ...] at ~1 ms
    slots, clicks, interval = body["slots"], body["clicks"], body["interval"]
    first = min(s["at"] for s in slots)
    noise = sorted(v for t, v in env if t < first - 0.3) or [0.002]
    floor = noise[len(noise) * 9 // 10]
    thr = max(floor * 5, 0.0015)
    out = {}
    for s in slots:
        delays, levels = [], []
        for k in range(clicks):
            e = s["at"] + k * interval
            win = [(t, v) for t, v in env if e - 0.3 <= t < e + 0.85]
            hit = next(((t, v) for t, v in win if v > thr), None)
            if not hit:
                continue
            delays.append(hit[0] - e)
            levels.append(max(v for t, v in win if hit[0] <= t < hit[0] + 0.03))
        if len(delays) >= 2:
            delays.sort(); levels.sort()
            out[s["device"]] = {"delay": delays[len(delays) // 2], "level": levels[len(levels) // 2],
                                "spread_ms": round((delays[-1] - delays[0]) * 1000)}
    return {"floor_db": round(20 * __import__("math").log10(max(floor, 1e-6)), 1), "devices": out}
SUBS_LOCK = threading.Lock()
DEVICES_FILE = paths.data("devices.json")


def publish(ev: dict):
    ev = {**ev, "server_time": time.time()}
    with SUBS_LOCK:
        for q in list(SUBS):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


def load_devices() -> dict:
    try:
        with open(DEVICES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
STATE_FILE = paths.data("state.json")
TOKEN_FILE = paths.data("token")
SCENES_FILE = paths.data("scenes.json")
DEFAULT_SCENES = {
    "Evening": {"brightness": 25, "cct": 2700, "gm": 0},
    "Work": {"brightness": 70, "cct": 4300, "gm": 0},
    "Daylight": {"brightness": 100, "cct": 5600, "gm": 0},
}


def load_scenes() -> dict:
    try:
        with open(SCENES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_SCENES)


def save_scenes(scenes: dict):
    with open(SCENES_FILE, "w") as f:
        json.dump(scenes, f, indent=2, ensure_ascii=False)
SETTERS = ("brightness", "cct", "gm")
COLOR = ("hue", "sat")


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"brightness": 50, "cct": 4000, "gm": 0, "on": True, "last_on_brightness": 50}


def _token() -> str:
    if not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(secrets.token_urlsafe(16))
        os.chmod(TOKEN_FILE, 0o600)
    with open(TOKEN_FILE) as f:
        return f.read().strip()


class Light:
    def __init__(self):
        self.driver = load_driver(paths.config().get("driver", "nanlite_mesh"))
        self.lock = asyncio.Lock()
        self.state = _load_state()
        self.last_used = 0.0
        self.alarm = {"state": "idle"}
        self.alarm_stopped: set = set()
        self.alarm_snooze_until = 0.0
        self.alarm_resume_at = None
        self.alarm_fade_in = 30.0
        self.alarm_snooze_at, self.alarm_snooze_level, self.alarm_wake_at = 0.0, 0, None
        self.alarm_test = None
        self.alarm_light_override = False

    def _connected(self) -> bool:
        return self.driver.connected()

    async def _connect(self):
        await self.driver.connect()

    async def _drop(self):
        await self.driver.disconnect()

    async def io(self, op):
        """Run a driver call with the link up; reconnect and retry once if the link dropped."""
        async with self.lock:
            for attempt in range(2):
                try:
                    if not self._connected():
                        await self._connect()
                    result = await op()
                    self.last_used = time.time()
                    return result
                except ValueError:
                    raise
                except Exception:
                    await self._drop()
                    if attempt:
                        raise

    async def apply(self, values: dict, source: str = "manual") -> dict:
        if source == "manual" and self.alarm.get("state") in ("dawn", "ringing", "snoozed"):
            self.alarm_light_override = True  # a person took the light: the alarm keeps only its sound
        if source == "manual":
            # A person or agent changed the light: the schedule waits for its next keyframe.
            s = sched.load()
            if s.get("enabled"):
                self.state["schedule_paused_until"] = sched.value_at(s)["next_point"]
        values = {k: int(v) for k, v in values.items()}
        st = self.state
        values = await self.io(lambda: self.driver.set(values, st))
        self.state.update(values)
        if "brightness" in values:
            b = int(values["brightness"])
            self.state["on"] = b > 0
            if b > 0:
                self.state["last_on_brightness"] = b
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)
        publish({"type": "state", "state": self.state})
        return self.state

    async def schedule_loop(self):
        last = None
        while True:
            await asyncio.sleep(10)
            try:
                s = sched.load()
                if (not s.get("enabled") or time.time() < self.state.get("schedule_paused_until", 0)
                        or self.alarm.get("state") != "idle"):
                    last = None
                    continue
                v = sched.value_at(s)
                level, kelvin = round(v["level"]), int(round(v["kelvin"] / 50) * 50)
                st = self.state
                if level <= 0:
                    want = None if not st.get("on") else {"brightness": 0}
                else:
                    want = {"brightness": level, "cct": kelvin}
                    if (st.get("on") and st.get("mode") != "hsi" and last == (level, kelvin)):
                        want = None
                if want:
                    await self.apply(want, source="schedule")
                last = (level, kelvin)
            except Exception as e:
                print(f"schedule: {type(e).__name__}: {e}", flush=True)

    def alarm_event(self) -> dict:
        return {"type": "alarm", **self.alarm}

    def stop_alarm(self, reason: str = "stop"):
        if self.alarm.get("key"):
            self.alarm_stopped.add(self.alarm["key"])
        self.alarm_test = None
        self.alarm_snooze_until, self.alarm_resume_at, self.alarm_wake_at = 0.0, None, None
        self.alarm_light_override = False
        self.alarm = {"state": "idle", "reason": reason}
        publish(self.alarm_event())

    def _current_alarm(self):
        if self.alarm_test:
            if time.time() < self.alarm_test["end"] and self.alarm_test["key"] not in self.alarm_stopped:
                return self.alarm_test
            self.alarm_test = None
        now = datetime.now().astimezone()
        d = alarms.load()
        for a in d.get("alarms", []):
            mode = d["modes"].get(a.get("mode"))
            t = alarms.occurrence(a, mode, now) if mode else None
            if not t:
                continue
            start, end = alarms.window(mode, t)
            key = f"{a['id']}@{t.isoformat()}"
            if start <= now < end and key not in self.alarm_stopped:
                return {"id": a["id"], "key": key, "T": t.timestamp(), "scale": 1.0, "mode": mode,
                        "mode_id": a["mode"], "sound": a.get("sound"), "end": end.timestamp()}
        return None

    async def alarm_loop(self):
        last_light = None
        while True:
            await asyncio.sleep(1)
            try:
                cur = self._current_alarm()
                if not cur:
                    if self.alarm.get("state") != "idle":
                        self.stop_alarm()
                    last_light = None
                    continue
                now = time.time()
                x = (now - cur["T"]) / 60 * cur["scale"]
                # Auto-stop: ring_minutes after ringing starts (after a snooze: after it resumes).
                ring_from = (self.alarm_resume_at - cur["T"]) / 60 * cur["scale"] if self.alarm_resume_at else 0
                if x - ring_from >= float(alarms.load().get("ring_minutes", alarms.RING_MINUTES)):
                    self.stop_alarm(reason="auto")
                    last_light = None
                    continue
                snoozed = now < self.alarm_snooze_until
                if self.alarm_resume_at and now > self.alarm_resume_at + max(60, self.alarm_fade_in):
                    self.alarm_resume_at, self.alarm_wake_at = None, None
                ringing = alarms.sound_at(cur["mode"], x, cur["sound"]) is not None
                info = {"state": "snoozed" if snoozed else ("ringing" if ringing else "dawn"),
                        "id": cur["id"], "key": cur["key"], "T": cur["T"], "scale": cur["scale"],
                        "mode_id": cur["mode_id"], "name": cur["mode"].get("name"), "sound": cur["sound"],
                        "sound_points": cur["mode"].get("sound", []), "end": cur["end"],
                        "snooze_until": self.alarm_snooze_until if snoozed else None,
                        "resume_at": self.alarm_resume_at, "fade_in": self.alarm_fade_in,
                        "snooze_at": self.alarm_snooze_at if snoozed else None, "wake_at": self.alarm_wake_at}
                keys = ("state", "key", "snooze_until", "resume_at")
                if any(info[k] != self.alarm.get(k) for k in keys):
                    self.alarm = info
                    publish(self.alarm_event())
                if self.alarm_light_override:
                    continue
                v = alarms.light_at(cur["mode"], x)
                if not v:
                    continue
                lv = max(0, round(v["level"]))
                want = ({"brightness": lv, "hue": round(v["hue"]) % 360, "sat": round(v["sat"])} if "hue" in v
                        else {"brightness": lv, "cct": int(round(v["kelvin"] / 50) * 50)})
                if snoozed:  # ease down to a third of the level at the snooze, over 20 s
                    p = min(1.0, (now - self.alarm_snooze_at) * cur["scale"] / 20)
                    lv = round(self.alarm_snooze_level * (1 - p * 2 / 3))
                elif self.alarm_resume_at and now < self.alarm_resume_at + self.alarm_fade_in:
                    p = (now - self.alarm_resume_at) / self.alarm_fade_in
                    dim = self.alarm_snooze_level / 3
                    lv = round(dim + (lv - dim) * p * p * (3 - 2 * p))  # smoothstep back up to the curve
                want["brightness"] = lv
                if lv == 0:
                    want = {"brightness": 0}
                if want != last_light:
                    await self.apply(want, source="alarm")
                    last_light = want
            except Exception as e:
                print(f"alarm: {type(e).__name__}: {e}", flush=True)

    async def idle_watch(self):
        # Keep the link warm: reconnect in the background so a press never waits for a scan.
        while True:
            if not self._connected() and not self.lock.locked():
                async with self.lock:
                    try:
                        await self._connect()
                    except Exception as e:
                        print(f"keepalive reconnect failed: {e}", flush=True)
            await asyncio.sleep(10)


def main():
    loop = asyncio.new_event_loop()
    Thread(target=loop.run_forever, daemon=True).start()
    light = Light()
    asyncio.run_coroutine_threadsafe(light.idle_watch(), loop)
    asyncio.run_coroutine_threadsafe(light.schedule_loop(), loop)
    asyncio.run_coroutine_threadsafe(light.alarm_loop(), loop)
    token = _token()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive: one round trip per /time sample
        def _reply(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self, q: dict) -> bool:
            if self.client_address[0] in ("127.0.0.1", "::1"):
                return True
            auth = self.headers.get("Authorization", "")
            given = q.get("token") or self.headers.get("X-Token") or (
                auth[7:] if auth.startswith("Bearer ") else "")
            return secrets.compare_digest(given, token)

        def do_POST(self):
            # MCP streamable HTTP (JSON responses): any agent that can reach the bridge Mac
            # connects to http://<bridge-host>.local:8787/mcp with "Authorization: Bearer <token>".
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            if u.path in ("/calibrate/result", "/device_status"):
                if not self._authed(q):
                    return self._reply(401, {"error": "token required"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                except (ValueError, json.JSONDecodeError):
                    return self._reply(400, {"error": "JSON body required"})
                if u.path == "/device_status":
                    STATUS[body.get("device", "?")] = {**body, "at": time.time()}
                    return self._reply(200, {"ok": True})
                with open("/tmp/cal_last.json", "w") as f:
                    json.dump(body, f)  # raw envelope for diagnostics
                res = analyse_calibration(body)
                found = res["devices"]
                if found:
                    max_delay = max(r["delay"] for r in found.values())
                    max_level = max(r["level"] for r in found.values())
                    devs = load_devices()
                    for name, r in found.items():
                        d = devs.get(name, {})
                        d.update(offset_ms=round((max_delay - r["delay"]) * 1000),
                                 gain=round(min(3.0, max_level / max(r["level"], 1e-6)), 2),
                                 delay_ms=round(r["delay"] * 1000), spread_ms=r["spread_ms"],
                                 level_db=round(20 * __import__("math").log10(max(r["level"], 1e-6)), 1),
                                 calibrated_at=time.time())
                        if name == body.get("recorder") and body.get("rssi") is not None:
                            d["rssi_ref"] = body["rssi"]
                        devs[name] = d
                    with open(DEVICES_FILE, "w") as f:
                        json.dump(devs, f, indent=2, ensure_ascii=False)
                return self._reply(200, res)
            if u.path in ("/alarms", "/devices"):
                if not self._authed(q):
                    return self._reply(401, {"error": "token required"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    if u.path == "/alarms":
                        cur = alarms.load()
                        cur.update({k: body[k] for k in ("alarms", "modes", "ring_minutes", "snooze_minutes") if k in body})
                        alarms.save(cur)
                        return self._reply(200, cur)
                    with open(DEVICES_FILE, "w") as f:
                        json.dump(body, f, indent=2, ensure_ascii=False)
                    return self._reply(200, body)
                except (ValueError, KeyError, TypeError) as e:
                    return self._reply(400, {"error": f"{u.path[1:]}: {e}"})
            if u.path not in ("/mcp", "/schedule"):
                return self._reply(404, {"error": "POST only on /mcp and /schedule"})
            if not self._authed(q):
                return self._reply(401, {"error": "token required"})
            if u.path == "/schedule":
                try:
                    new = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    cur = sched.load()
                    cur.update({k: new[k] for k in ("enabled", "lat", "lon", "points") if k in new})
                    sched.save(cur)
                except (ValueError, KeyError, TypeError) as e:
                    return self._reply(400, {"error": f"schedule: {e}"})
                light.state.pop("schedule_paused_until", None)
                return self._reply(200, sched.info(cur))
            from . import mcp as nanlite_mcp
            try:
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except (ValueError, json.JSONDecodeError):
                return self._reply(400, {"error": "JSON-RPC body required"})
            out = ([r for r in map(nanlite_mcp.handle, msg) if r] if isinstance(msg, list)
                   else nanlite_mcp.handle(msg))
            if not out:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._reply(200, out)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            local = self.client_address[0] in ("127.0.0.1", "::1")
            if u.path in ("/manifest.webmanifest", "/icon.png", "/apple-touch-icon.png", "/favicon.ico"):
                name = "icon.png" if u.path == "/favicon.ico" else u.path[1:]
                data = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", name), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/manifest+json" if name.endswith("manifest") else "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            if not self._authed(q):
                return self._reply(401, {"error": "token required"})
            q.pop("token", None)
            if u.path in ("/", "/app", "/alarm"):
                # One phone page: remote by day, night mode inside it. /alarm is an old alias of /app.
                page = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web",
                                         "app.html"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if u.path == "/ping":
                # Short sound on one device or all of them: find a phone, test speakers, wake someone.
                with SUBS_LOCK:
                    online = sorted(k for k, n in ONLINE.items() if n > 0)
                target = q.get("device", "all")
                if target != "all" and target not in online:
                    return self._reply(404, {"error": "device not online", "online": online})
                ev = {"type": "ping", "device": target, "sound": q.get("sound", "chime"),
                      "volume": max(0.05, min(1.0, float(q.get("volume", 0.8))))}
                publish(ev)
                return self._reply(200, {**ev, "online": online})
            if u.path == "/links":
                # Phone URLs for QR codes: Tailscale HTTPS (permanent, mic allowed) if served, else LAN + token.
                import socket
                import subprocess
                host = socket.gethostname()
                host = host if host.endswith(".local") else host + ".local"
                links = {"lan": f"http://{host}:{self.server.server_address[1]}", "token": token}
                try:  # the Tailscale app's CLI cannot run under launchd ("GUI failed to start"): use the file
                    with open(os.path.join(os.path.dirname(STATE_FILE), "links.json")) as f:
                        links.update(json.load(f))
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                for ts in () if "tailscale" in links else ("/Applications/Tailscale.app/Contents/MacOS/Tailscale", "tailscale"):
                    try:
                        r = subprocess.run([ts, "serve", "status"], capture_output=True, text=True, timeout=5)
                        serve = r.stdout
                        links["tailscale_note"] = (r.stderr or f"rc {r.returncode}: {r.stdout[:160]!r}").strip()[:300]
                    except Exception as e:
                        links["tailscale_note"] = f"{type(e).__name__}: {e}"[:200]
                        continue
                    head = None
                    for line in serve.splitlines():
                        if line.startswith("https://"):
                            head = line.split()[0].rstrip("/")
                        elif head and f"proxy http://127.0.0.1:{self.server.server_address[1]}" in line:
                            links["tailscale"] = head
                            break
                    break
                return self._reply(200, links)
            if u.path == "/time":
                return self._reply(200, {"t": time.time()})
            if u.path == "/devices":
                with SUBS_LOCK:
                    online = sorted(k for k, n in ONLINE.items() if n > 0)
                return self._reply(200, {**load_devices(), "_online": online, "_status": STATUS})
            if u.path in ("/calibrate/ble", "/calibrate/reset"):
                # Bluetooth: each Mac's RSSI to the bridge beacon -> distance (1 m = -59 dBm, n = 2).
                # The listener is assumed next to that Mac, so it waits distance / 343 m/s for the bridge Mac's sound.
                devs = load_devices()
                report = {}
                for name, st in list(STATUS.items()):
                    d = devs.get(name, {})
                    if u.path == "/calibrate/reset":
                        d.update(offset_ms=0, gain=1.0, method="reset")
                    elif st.get("beacon"):
                        d.update(offset_ms=0, method="bluetooth (beacon)")
                    elif st.get("rssi") is not None:
                        metres = 10 ** ((-59 - st["rssi"]) / 20)
                        d.update(offset_ms=round(metres / 343 * 1000), rssi_ref=st["rssi"],
                                 distance_m=round(metres, 1), method="bluetooth")
                    else:
                        continue
                    d["calibrated_at"] = time.time()
                    devs[name] = d
                    report[name] = {k: d.get(k) for k in ("offset_ms", "gain", "distance_m", "method")}
                with open(DEVICES_FILE, "w") as f:
                    json.dump(devs, f, indent=2, ensure_ascii=False)
                return self._reply(200, report)
            if u.path == "/calibrate":
                # Acoustic calibration: every target plays 4 clicks in its own slot; the recorder
                # (a Mac with a mic near the bed) measures arrival time and level of each.
                with SUBS_LOCK:
                    online = sorted(k for k, n in ONLINE.items() if n > 0)
                targets = [t for t in q.get("targets", ",".join(online)).split(",") if t in online]
                recorder = q.get("recorder", "")
                if recorder not in online or not targets:
                    return self._reply(400, {"error": "recorder and targets must be online", "online": online})
                t0 = time.time() + 4
                slots = [{"device": d, "at": t0 + i * 6.0} for i, d in enumerate(targets)]
                ev = {"type": "calibrate", "id": f"cal{int(t0)}", "recorder": recorder,
                      "slots": slots, "clicks": 4, "interval": 1.0}
                publish(ev)
                return self._reply(200, ev)
            if u.path == "/alarms":
                d = alarms.load()
                now = datetime.now().astimezone()
                nxt = []
                for a in d.get("alarms", []):
                    m = d["modes"].get(a.get("mode"))
                    t = alarms.occurrence(a, m, now) if m else None
                    nxt.append({"id": a["id"], "next": t.isoformat() if t else None})
                return self._reply(200, {**d, "sounds": alarms.sounds(), "next": nxt,
                                         "active": light.alarm, "snooze_min": d["snooze_minutes"]})
            if u.path == "/alarm/stop":
                light.stop_alarm()
                return self._reply(200, light.alarm)
            if u.path == "/alarm/snooze":
                if light.alarm.get("state") in ("dawn", "ringing"):
                    sc = light.alarm.get("scale", 1.0)
                    mins = max(1.0, min(30.0, float(q.get("minutes") or alarms.load()["snooze_minutes"])))
                    total = mins * 60 / sc
                    rise = min(alarms.SNOOZE_RISE * 60 / sc, total * 0.6)
                    now = time.time()
                    light.alarm_snooze_at, light.alarm_snooze_level = now, int(light.state.get("brightness") or 0)
                    light.alarm_snooze_until = light.alarm_resume_at = now + total - rise
                    light.alarm_fade_in, light.alarm_wake_at = rise, now + total
                    if light.alarm_test:  # a test alarm runs on past the snooze
                        light.alarm_test["end"] += total
                return self._reply(200, {"snooze_until": light.alarm_snooze_until})
            if u.path == "/alarm/test":
                # Whole alarm compressed into `seconds` (default 90), like Philips demo mode.
                d = alarms.load()
                mid = q.get("mode", "gentle")
                mode = d["modes"].get(mid)
                if not mode:
                    return self._reply(404, {"error": f"modes: {', '.join(d['modes'])}"})
                secs = max(20.0, float(q.get("seconds", 90)))
                first = min(p["t"] for p in mode["light"] + mode["sound"])
                span = -first + 5  # until 5 min after the alarm time
                scale = span * 60 / secs
                now = time.time()
                light.alarm_light_override = False
                light.alarm_test = {"id": "test", "key": f"test@{now}", "T": now - first * 60 / scale,
                                    "scale": scale, "mode": mode, "mode_id": mid,
                                    "sound": q.get("sound") or "birds-spain", "end": now + secs}
                return self._reply(200, {"test": mid, "seconds": secs, "scale": round(scale, 2)})
            if u.path.startswith("/sounds/"):
                name = os.path.basename(u.path)
                f = os.path.join(alarms.SOUNDS_DIR, name)
                if not name.endswith(".m4a") or not os.path.isfile(f):
                    return self._reply(404, {"error": "no such sound", "sounds": alarms.sounds()})
                data = open(f, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/events":
                # Server-sent events: alarm + light state pushed to every device the moment they change.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                qq = queue.Queue(maxsize=200)
                dev = q.get("device", "")
                with SUBS_LOCK:
                    SUBS.append(qq)
                    ONLINE[dev] = ONLINE.get(dev, 0) + 1
                try:
                    for ev in (light.alarm_event(), {"type": "state", "state": light.state}):
                        self.wfile.write(f"data: {json.dumps({**ev, 'server_time': time.time()})}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            self.wfile.write(f"data: {json.dumps(qq.get(timeout=15))}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    with SUBS_LOCK:
                        SUBS.remove(qq)
                        ONLINE[dev] = ONLINE.get(dev, 1) - 1
                return
            if u.path.startswith("/schedule"):
                s = sched.load()
                if u.path == "/schedule/enable":
                    s["enabled"] = q.get("on", "1") not in ("0", "false", "off")
                    sched.save(s)
                    light.state.pop("schedule_paused_until", None)
                elif u.path == "/schedule/resume":
                    light.state.pop("schedule_paused_until", None)
                elif u.path == "/schedule/preview":
                    return self._reply(200, sched.preview(s, int(q.get("step", 10))))
                elif u.path != "/schedule":
                    return self._reply(404, {"error": "/schedule /schedule/enable?on=1|0 /schedule/resume /schedule/preview"})
                out = sched.info(s)
                out["paused_until"] = light.state.get("schedule_paused_until")
                return self._reply(200, out)
            if u.path == "/scenes":
                return self._reply(200, load_scenes())
            if u.path in ("/scene/save", "/scene/delete"):
                name = q.get("name", "").strip()
                if not name:
                    return self._reply(400, {"error": "name= required"})
                scenes = load_scenes()
                if u.path == "/scene/delete":
                    scenes.pop(name, None)
                else:
                    st = light.state
                    keys = ("hue", "sat") if st.get("mode") == "hsi" else ("cct", "gm")
                    scenes[name] = {"brightness": st.get("last_on_brightness", 50),
                                    **{k: st.get(k) for k in keys}}
                save_scenes(scenes)
                return self._reply(200, scenes)
            if u.path == "/state":
                return self._reply(200, light.state)
            if u.path == "/raw":
                if not local:
                    return self._reply(403, {"error": "/raw is localhost only"})
                try:
                    payload = bytes.fromhex(q.get("hex", ""))
                except ValueError:
                    return self._reply(400, {"error": "hex= must be hex bytes"})
                fut = asyncio.run_coroutine_threadsafe(light.io(lambda: light.driver.raw(payload)), loop)
                try:
                    fut.result(timeout=40)
                    return self._reply(200, {"sent": payload.hex()})
                except Exception as e:
                    return self._reply(503, {"error": f"{type(e).__name__}: {e}"})
            if u.path == "/scene":
                scene = load_scenes().get(q.get("name", ""))
                if scene is None:
                    return self._reply(404, {"error": "unknown scene", "scenes": list(load_scenes())})
                values = dict(scene)
            elif u.path == "/off":
                values = {"brightness": 0}
            elif u.path == "/on":
                values = {"brightness": light.state.get("last_on_brightness", 50)}
            elif u.path == "/toggle":
                values = {"brightness": 0 if light.state.get("on") else
                          light.state.get("last_on_brightness", 50)}
            elif u.path == "/set":
                values = {k: v for k, v in q.items() if k in SETTERS or k in COLOR}
                if not values:
                    return self._reply(400, {"error": "use brightness, cct, gm, hue, sat"})
                if any(k in values for k in COLOR) and ("cct" in values or "gm" in values):
                    return self._reply(400, {"error": "hue/sat and cct/gm are different modes"})
            else:
                return self._reply(404, {"error": "paths: /state /on /off /toggle /set /scenes /scene /scene/save /scene/delete, POST /mcp"})
            fut = asyncio.run_coroutine_threadsafe(light.apply(values), loop)
            try:
                self._reply(200, fut.result(timeout=40))
            except ValueError as e:
                self._reply(400, {"error": str(e)})
            except Exception as e:
                self._reply(503, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, fmt, *args):
            line = re.sub(r"token=[^&\s\"]*", "token=***", fmt % args)
            print(f"{time.strftime('%F %T')} {self.client_address[0]} {line}", flush=True)

    host = os.environ.get("TINYLIGHT_HOST", "0.0.0.0")
    port = int(os.environ.get("TINYLIGHT_PORT", paths.config().get("port", 8787)))
    print(f"tinyLight bridge ({light.driver.title}) on http://{host}:{port}", flush=True)
    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True  # long-lived /events streams must not block shutdown
    srv.serve_forever()


if __name__ == "__main__":
    main()
