# Connect any Bluetooth light

The order that worked for the Nanlite light. Each step has a clear stop: do not move on until it passes.

## 1. Find it and fingerprint it

```bash
tinylight scan            # in Terminal.app; lists devices, services and the matching driver
tinylight probe <address> # GATT table -> probes/<address>.json
```

Stop when you know: the advertised services (16-bit UUIDs like `1827`, `ffe0`), the GATT services and
which characteristics are `write` / `write-without-response` / `notify`.

Known fingerprints:

| Looks like | Probably |
|---|---|
| `1827` / `1828` advertised | Bluetooth mesh (provisioning / proxy). Needs keys: pair it yourself or extract keys from the vendor app |
| `fff0` + `fff1` notify + `fff2` write | Feasycom module (often a custom auth handshake first) |
| `ffe0` + `ffe1` | Generic serial-over-BLE modules (HM-10 style): often plain packets, easiest case |
| Vendor service + "AT" text in the name | Module firmware leaking AT commands; the name tells you the chip vendor |

## 2. Look for prior work first

Search GitHub for the exact service UUIDs, the advertised name and the vendor app's package name.
For Nanlite three projects existed (FS-300B, PavoTube, Wand); together they covered 90 % of the protocol.
Stop when you have read every hit, or after a clean miss.

## 3. Read the vendor app or capture its traffic

- **Read the app.** Download the Android APK and decompile it (JADX, or an agent with
  [REA](https://github.com/morluto/rea)). Search for the service UUIDs, `writeCharacteristic`, packet
  builders, and native libraries (`lib*.so`) for keys.
- **Capture.** Android: Developer options -> *Bluetooth HCI snoop log*, move one slider at a time, open the
  log in Wireshark. Apple: PacketLogger (Additional Tools for Xcode) with a Bluetooth logging profile.

Stop when you can say which bytes change when you move brightness from 10 % to 60 %.

## 4. Write the driver

```bash
tinylight new-driver mylight     # copies drivers/template.py
tinylight try mylight brightness 10
tinylight try mylight brightness 60
```

Implement in this order and test each on the real light: connect (+ auth) -> brightness -> off ->
white temperature -> tint -> color. Return `mode="cct"` or `mode="hsi"` from `set()` when the mode changes.
A command that is accepted but changes nothing is a failure: watch the light, do not trust the ACK.

## 5. Run it

Add the driver to `tinylight/drivers/__init__.py`, set `"driver"` in `~/.tinylight/config.json`,
then `tinylight install`. Scenes, the daily curve, alarms, MCP, Apple Home, the phone page and the menu bar
app work unchanged.

## Lessons from the Nanlite run

- An ACK proves delivery, not effect. The light acknowledged options that did nothing.
- Encodings differ between models of the same brand (G/M: 0-100 with 50 neutral on one, signed with
  0 neutral on another). The wrong one silently tints the light.
- Some settings only exist in a longer "set mode" packet, not in the short per-parameter command.
- Keep a decisions log with measured numbers; most wrong turns came from one unmeasured assumption.
