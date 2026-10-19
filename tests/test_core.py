"""Core checks without hardware: easing, sunset math, alarm rules, and the bridge on the dummy driver.
Run: python3 -m pytest tests  (or python3 tests/test_core.py)"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date

HOME = tempfile.mkdtemp()
os.environ["TINYLIGHT_HOME"] = HOME
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tinylight import alarm, schedule  # noqa: E402


def test_bezier_ends_and_linear():
    assert schedule.bezier(0, 0.5, 0.5) < 0.01
    assert schedule.bezier(1, 0.5, 0.5) > 0.99
    assert abs(schedule.bezier(0.25, 0, 1) - 0.25) < 0.01      # 0/0 influence = linear


def test_sunset_london():
    # London: about 21:21 on 21 June (BST) and 15:53 on 21 December (GMT), local clock
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    assert abs(schedule.sun_minutes(date(2026, 6, 21), 51.507, -0.128) - (21 * 60 + 21)) <= 3
    assert abs(schedule.sun_minutes(date(2026, 12, 21), 51.507, -0.128) - (15 * 60 + 53)) <= 3


def test_alarm_morning_only():
    d = {"alarms": [{"id": "a", "time": "12:30", "mode": "gentle", "days": [0]}], "modes": alarm.MODES}
    try:
        alarm.validate(d)
        assert False, "afternoon alarm accepted"
    except ValueError:
        pass


def test_alarm_dawn_starts_amber():
    v = alarm.light_at(alarm.MODES["gentle"], -30)
    assert "hue" in v and v["level"] <= 2


def test_bridge_with_dummy_driver():
    with open(os.path.join(HOME, "config.json"), "w") as f:
        json.dump({"driver": "dummy"}, f)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, "TINYLIGHT_HOST": "127.0.0.1", "TINYLIGHT_PORT": str(port)}
    p = subprocess.Popen([sys.executable, "-m", "tinylight.server"], env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/time", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        st = json.load(urllib.request.urlopen(url + "/set?brightness=40&cct=3000"))
        assert st["brightness"] == 40 and st["mode"] == "cct"
        st = json.load(urllib.request.urlopen(url + "/set?hue=200&sat=90"))
        assert st["mode"] == "hsi"
        tools = json.load(urllib.request.urlopen(urllib.request.Request(
            url + "/mcp", data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')))
        assert len(tools["result"]["tools"]) >= 12
        assert urllib.request.urlopen(url + "/app").status == 200
    finally:
        p.terminate()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
