"""Apple Home accessory "Room light" for the Nanlite light, via the nanlited bridge.

Runs next to the bridge (LaunchAgent com.tinylight.homekit). Pairing code + URI: $TINYLIGHT_HOME/homekit/PAIRING.txt.
Home app -> Add Accessory -> More options -> "Room light" -> enter the code.
"""

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_LIGHTBULB

BRIDGE = os.environ.get("NANLITE_URL", "http://127.0.0.1:8787")
from tinylight import paths

STATE_DIR = paths.data("homekit")
MIRED_MIN, MIRED_MAX = 154, 370  # 6500 K .. 2700 K

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def bridge(path: str, **q) -> dict:
    url = BRIDGE + path + ("?" + urllib.parse.urlencode(q) if q else "")
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


class DnsSdAdvertiser:
    """Publish the HAP service through the system mDNSResponder (`dns-sd -R`).

    HAP-python's own zeroconf multicast gets EHOSTUNREACH under launchd on macOS 15
    (Local Network privacy), so iPhones never saw the accessory. mDNSResponder is exempt.
    """

    def __init__(self):
        self.proc = None

    async def async_register_service(self, info, **_):
        await self.async_unregister_service(info)
        txt = [f"{k.decode()}={v.decode()}" for k, v in info.properties.items()]
        name = info.name.split("._hap._tcp")[0]
        self.proc = await asyncio.create_subprocess_exec(
            "/usr/bin/dns-sd", "-R", name, "_hap._tcp", "local", str(info.port), *txt,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        logging.info("mDNS via dns-sd: %s %s", name, " ".join(txt))

    async def async_update_service(self, info):
        await self.async_register_service(info)

    async def async_unregister_service(self, info=None):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()
        self.proc = None

    async def async_close(self):
        await self.async_unregister_service()


class RoomLight(Accessory):
    category = CATEGORY_LIGHTBULB

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_info_service(manufacturer="tinyLight", model="Bluetooth light via tinyLight",
                              serial_number="nanlited-0002", firmware_revision="0.1.0")
        serv = self.add_preload_service(
            "Lightbulb", chars=["On", "Brightness", "ColorTemperature", "Hue", "Saturation"])
        self.c_on = serv.get_characteristic("On")
        self.c_bri = serv.get_characteristic("Brightness")
        self.c_ct = serv.get_characteristic("ColorTemperature")
        self.c_ct.override_properties(properties={"minValue": MIRED_MIN, "maxValue": MIRED_MAX})
        self.c_hue = serv.get_characteristic("Hue")
        self.c_sat = serv.get_characteristic("Saturation")
        serv.setter_callback = self.on_set  # one call per Home action, all changed chars
        # Second tile: the daily schedule on/off (Home scenes, automations, Shortcuts, Siri).
        sw = self.add_preload_service("Switch", chars=["Name"])
        sw.configure_char("Name", value="Light schedule")
        self.c_sched = sw.configure_char("On", setter_callback=self.on_schedule)
        # Third tile: "Alarm" is on while an alarm runs; switching it off stops it on every device.
        al = self.add_preload_service("Switch", chars=["Name"])
        al.configure_char("Name", value="Alarm")
        self.c_alarm = al.configure_char("On", setter_callback=self.on_alarm)
        self.last_set = 0.0

    def on_set(self, chars: dict):
        self.last_set = time.time()
        try:
            # HAP delivers On as 0/1, not bool; Brightness 0 also means off.
            if ("On" in chars and not chars["On"]) or chars.get("Brightness", 1) == 0:
                bridge("/off")
                return
            q = {}
            if "Brightness" in chars:
                q["brightness"] = int(chars["Brightness"])
            if "Hue" in chars or "Saturation" in chars:
                q["hue"] = int(chars.get("Hue", self.c_hue.value)) % 360
                q["sat"] = int(chars.get("Saturation", self.c_sat.value))
            elif "ColorTemperature" in chars:
                k = 1_000_000 / chars["ColorTemperature"]
                q["cct"] = int(min(6500, max(2700, round(k / 50) * 50)))
            if q:
                bridge("/set", **q)
            elif chars.get("On"):
                bridge("/on")
        except Exception as e:  # report to the log, Home shows "No response" on its own
            logging.warning("set %s failed: %s", chars, e)

    def on_schedule(self, value):
        self.last_set = time.time()
        try:
            bridge("/schedule/enable", on="1" if value else "0")
        except Exception as e:
            logging.warning("schedule switch failed: %s", e)

    def on_alarm(self, value):
        self.last_set = time.time()
        if not value:
            try:
                bridge("/alarm/stop")
            except Exception as e:
                logging.warning("alarm stop failed: %s", e)

    @Accessory.run_at_interval(3)
    def run(self):
        # Mirror changes from the menu bar app, agents and the CLI.
        if time.time() - self.last_set < 3:
            return
        try:
            s = bridge("/state")
        except Exception:
            return
        try:
            self.c_sched.set_value(bool(bridge("/schedule").get("enabled")))
            self.c_alarm.set_value(bridge("/alarms").get("active", {}).get("state", "idle") != "idle")
        except Exception:
            pass
        self.c_on.set_value(bool(s.get("on")))
        self.c_bri.set_value(int(s.get("last_on_brightness") or s.get("brightness") or 50))
        if s.get("mode") == "hsi":
            self.c_hue.set_value(int(s.get("hue", 0)) % 360)
            self.c_sat.set_value(int(s.get("sat", 100)))
        else:
            m = round(1_000_000 / int(s.get("cct", 4000)))
            self.c_ct.set_value(min(MIRED_MAX, max(MIRED_MIN, m)))


async def amain():
    # driver.start() uses asyncio.SafeChildWatcher, removed in Python 3.14: run our own loop.
    os.makedirs(STATE_DIR, exist_ok=True)
    driver = AccessoryDriver(port=51826, persist_file=os.path.join(STATE_DIR, "room-light.state"),
                             loop=asyncio.get_running_loop(),
                             async_zeroconf_instance=DnsSdAdvertiser())
    acc = RoomLight(driver, "Room light")
    driver.add_accessory(accessory=acc)
    with open(os.path.join(STATE_DIR, "PAIRING.txt"), "w") as f:
        f.write(f"Setup code: {driver.state.pincode.decode()}\nSetup URI: {acc.xhm_uri()}\n")
    await driver.async_start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(amain())
