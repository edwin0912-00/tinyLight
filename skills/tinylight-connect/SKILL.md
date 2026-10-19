---
name: tinylight-connect
description: Connect any Bluetooth light to tinyLight (bridge, MCP, Apple Home, menu bar app, schedule, alarms) - scan, fingerprint, reuse prior work, reverse-engineer, write and test a driver. Use when the user wants to control a Bluetooth lamp without its vendor app, add a new light model to tinyLight, or debug a tinyLight driver.
---

# tinyLight: connect a Bluetooth light

Repo: tinyLight (this skill lives in `skills/tinylight-connect`). Full guide: `docs/connect-any-light.md`.
Every step ends with a check. Do not start the next step until the check passes.

## Before anything

1. **Bluetooth access.** Run BLE code from Terminal.app or a LaunchAgent. Over SSH or from an agent
   sandbox bleak fails with "Bluetooth is not authorized" (macOS exit 134 / TCC). Check:
   `tinylight scan` lists nearby devices.
2. **One controller.** A connected light stops advertising. Stop the bridge
   (`launchctl bootout gui/$UID ~/Library/LaunchAgents/com.tinylight.bridge.plist`) before scanning for it.

## Steps

1. **Scan and fingerprint**: `tinylight scan`, then `tinylight probe <address>`.
   Check: you can name the advertised services and the write/notify characteristics.
2. **Known driver?** A score >= 0.5 in `tinylight scan` means yes -> `tinylight connect`
   (asks location, pairs, tests 10 % / 60 %). Check: the owner confirms the light dimmed and brightened.
3. **Prior work**: search GitHub for the service UUIDs, the advertised name and the vendor app package.
   Check: every hit read, or a clean miss written down.
4. **Protocol**: read the vendor Android app (JADX / REA on the APK; native `lib*.so` for keys) or capture
   HCI traffic while moving one slider at a time. Check: you know which bytes change for brightness.
5. **Driver**: `tinylight new-driver <name>`; implement connect(+auth) -> brightness -> off -> CCT -> tint
   -> color; `tinylight try <name> brightness 10`. Check: the owner sees each change.
   An ACK is not proof: some lights acknowledge commands that do nothing.
6. **Run**: register in `tinylight/drivers/__init__.py`, set `"driver"` in `~/.tinylight/config.json`,
   `tinylight install`, `tinylight doctor`. Check: `/state` answers and the menu bar app moves the light.

## Rules

- Ask before downloading a vendor APK, pairing (it removes the light from the vendor app) or sending raw probes
  while someone is in the room.
- Never commit keys, the bridge token or `$TINYLIGHT_HOME` files.
- Write every finding with its measured numbers into `docs/<driver>.md`.
