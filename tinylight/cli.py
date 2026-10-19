"""tinylight - connect any Bluetooth light and run it from agents, Apple Home and the menu bar.

  tinylight scan                 lights nearby, with the driver each one matches
  tinylight connect              guided setup: pick a light, match a driver, pair, test, write config
  tinylight probe <address>      dump the GATT table of an unknown light (start of reverse engineering)
  tinylight new-driver <name>    start a driver for a new light family from the template
  tinylight try <driver> <key> <value>   send one command directly (brightness 10, cct 3000, hue 240)
  tinylight install              run the bridge (+ Apple Home) at login via LaunchAgents
  tinylight doctor               check Bluetooth, bridge, driver link, clock and connectors

macOS: Bluetooth works from Terminal.app (or a LaunchAgent) with permission. A process started over SSH
or by some IDEs gets no Bluetooth access (bleak raises "Bluetooth is not authorized").
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import urllib.request

from . import paths
from .drivers import DRIVERS, all_classes, driver_class


def ask(q: str, default: str = "") -> str:
    a = input(f"{q}{f' [{default}]' if default else ''}: ").strip()
    return a or default


async def scan(timeout: float = 10.0) -> list[dict]:
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    classes = all_classes()
    rows = []
    for addr, (d, adv) in found.items():
        name = d.name or adv.local_name
        services = {str(u).lower() for u in adv.service_uuids}
        best = max(((c.match(name, services, dict(adv.manufacturer_data)), c) for c in classes),
                   key=lambda t: t[0], default=(0, None))
        rows.append({"address": addr, "name": name, "rssi": adv.rssi, "services": sorted(services),
                     "driver": best[1].name if best[0] >= 0.5 else None, "score": round(best[0], 2)})
    return sorted(rows, key=lambda r: (-r["score"], -r["rssi"]))


def cmd_scan():
    rows = asyncio.run(scan())
    for r in rows:
        if r["score"] or r["services"]:
            mark = f"-> {r['driver']} ({r['score']})" if r["driver"] else ""
            print(f"{r['rssi']:>4} dBm  {r['address']}  {repr(r['name'])[:28]:<30} {','.join(s[4:8] for s in r['services'])} {mark}")


async def probe(address: str) -> dict:
    from bleak import BleakClient
    out = {"address": address, "services": []}
    async with BleakClient(address, timeout=20) as c:
        for s in c.services:
            svc = {"uuid": s.uuid, "description": s.description, "characteristics": []}
            for ch in s.characteristics:
                v = None
                if "read" in ch.properties:
                    try:
                        v = (await c.read_gatt_char(ch)).hex()
                    except Exception as e:
                        v = f"error: {e}"
                svc["characteristics"].append({"uuid": ch.uuid, "properties": ch.properties, "value": v})
            out["services"].append(svc)
    return out


def cmd_probe(address: str):
    res = asyncio.run(probe(address))
    os.makedirs(os.path.join(paths.REPO, "probes"), exist_ok=True)
    f = os.path.join(paths.REPO, "probes", f"{address.replace(':', '')}.json")
    with open(f, "w") as fh:
        json.dump(res, fh, indent=2)
    for s in res["services"]:
        print(f"service {s['uuid']} {s['description']}")
        for ch in s["characteristics"]:
            print(f"   {ch['uuid']} {','.join(ch['properties'])} {ch['value'] or ''}")
    print(f"\nSaved {f}. Next: search GitHub for these UUIDs and the name; then see docs/connect-any-light.md.")


def cmd_new_driver(name: str):
    dst = os.path.join(paths.REPO, "tinylight", "drivers", f"{name}.py")
    if os.path.exists(dst):
        sys.exit(f"{dst} exists")
    src = open(os.path.join(paths.REPO, "tinylight", "drivers", "template.py")).read()
    cls = "".join(p.title() for p in name.split("_")) + "Driver"
    open(dst, "w").write(src.replace("TemplateDriver", cls).replace('name = "template"', f'name = "{name}"'))
    print(f"Created {dst}.\nRegister it in tinylight/drivers/__init__.py:\n"
          f'    "{name}": "tinylight.drivers.{name}:{cls}",')


def cmd_try(driver: str, key: str, value: str):
    async def run():
        d = driver_class(driver)()
        await d.connect()
        print(await d.set({key: int(value)}, {"on": True, "last_on_brightness": 50}))
        await d.disconnect()
    asyncio.run(run())


def cmd_connect():
    print("Scanning for Bluetooth lights (10 s)…")
    rows = [r for r in asyncio.run(scan()) if r["services"] or r["score"]]
    for i, r in enumerate(rows[:15]):
        print(f"{i:>2}. {r['rssi']:>4} dBm {repr(r['name'])[:26]:<28} {r['driver'] or 'unknown':<14} {r['address']}")
    if not rows:
        sys.exit("No Bluetooth devices with services found. Is the light on, and does this process have Bluetooth access?")
    pick = rows[int(ask("Number of your light", "0"))]
    drv = pick["driver"]
    if not drv:
        print("\nNo driver knows this light yet. Reading its GATT table…")
        cmd_probe(pick["address"])
        print("Then: tinylight new-driver <name>, and follow docs/connect-any-light.md.")
        return
    cls = driver_class(drv)
    print(f"\nLooks like: {cls.title} (driver {drv}, score {pick['score']}).")
    cfg = paths.config()
    cfg["driver"] = drv
    city = ask("Latitude,longitude for sunrise/sunset (e.g. 39.47,-0.376)",
               f"{cfg.get('lat', '')},{cfg.get('lon', '')}".strip(","))
    if "," in city:
        cfg["lat"], cfg["lon"] = (float(x) for x in city.split(",")[:2])
    paths.save_config(cfg)
    unpaired = any(s.startswith("00001827") for s in pick["services"]) and drv == "nanlite_mesh"
    if unpaired or ask("Pair now? The light must be reset / removed from the vendor app (y/n)", "n") == "y":
        asyncio.run(cls.pair())
    print("Test: the light goes to 10 %, then 60 %.")
    cmd_try(drv, "brightness", "10")
    cmd_try(drv, "brightness", "60")
    if ask("Did the light dim and brighten (y/n)", "y") != "y":
        sys.exit("Driver did not control the light. Run `tinylight doctor` and see docs/connect-any-light.md.")
    print(f"\nSaved {paths.data('config.json')}. Next: tinylight install")


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>{python}</string><string>-u</string><string>-m</string><string>{module}</string></array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>EnvironmentVariables</key><dict><key>TINYLIGHT_HOME</key><string>{home}</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{home}/{log}</string><key>StandardErrorPath</key><string>{home}/{log}</string>
</dict></plist>
"""


def cmd_install():
    agents = os.path.expanduser("~/Library/LaunchAgents")
    for label, module, log in (("com.tinylight.bridge", "tinylight.server", "bridge.log"),
                               ("com.tinylight.homekit", "tinylight.homekit", "homekit.log")):
        f = os.path.join(agents, f"{label}.plist")
        open(f, "w").write(PLIST.format(label=label, python=sys.executable, module=module, repo=paths.REPO,
                                        home=paths.HOME, log=log))
        os.system(f"launchctl bootout gui/{os.getuid()} {f} 2>/dev/null; launchctl bootstrap gui/{os.getuid()} {f}")
        print(f"loaded {label}")
    print("macOS may ask for Bluetooth access for Python: allow it.\n"
          "Agents: claude mcp add -s user room-light -- " + sys.executable + " -m tinylight.mcp\n"
          "Apple Home: pairing code in " + paths.data("homekit/PAIRING.txt"))


def cmd_doctor():
    port = paths.config().get("port", 8787)
    print("config :", paths.config() or "missing - run tinylight connect")
    try:
        st = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=5))
        print("bridge : up, state", {k: st.get(k) for k in ("on", "brightness", "mode")})
    except OSError as e:
        print("bridge : down -", e, "(tinylight install)")
    try:
        rows = asyncio.run(scan(5))
        print(f"bluetooth: ok, {len(rows)} devices around")
    except Exception as e:
        print("bluetooth:", e)
    print("mcp    :", "claude" if shutil.which("claude") else "-", "codex" if shutil.which("codex") else "-")


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmds = {"scan": cmd_scan, "connect": cmd_connect, "install": cmd_install, "doctor": cmd_doctor,
            "probe": lambda: cmd_probe(a[1]), "new-driver": lambda: cmd_new_driver(a[1]),
            "try": lambda: cmd_try(a[1], a[2], a[3])}
    if a[0] not in cmds:
        sys.exit(f"unknown command {a[0]}; drivers: {', '.join(DRIVERS)}")
    cmds[a[0]]()


if __name__ == "__main__":
    main()
