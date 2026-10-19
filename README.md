# tinyLight

Run a Bluetooth light without its vendor app: from AI agents (MCP), Apple Home, a macOS menu bar app,
a phone page, a daily light curve and sunrise alarms. One Mac keeps the Bluetooth link; everything
else talks to it over the network.

Built on a Nanlite light (Feasycom Bluetooth mesh). Other lights plug in as drivers, and the
`tinylight connect` helper walks you from "unknown light" to a working driver.

## What you get

| Piece | What it does |
|---|---|
| **Bridge** (`tinylight.server`) | Owns the Bluetooth link, keeps it warm (commands in ~0.1-0.2 s), HTTP API, push events (`/events`) |
| **Drivers** (`tinylight/drivers`) | One file per light family behind one interface. `nanlite_mesh` (works), `dummy` (no hardware), `template` |
| **Scenes** | Named light settings, from any client |
| **Daily curve** | 24 h brightness + warmth curve with After Effects style easing; points can follow sunset/sunrise |
| **Sunrise alarm** | 5 modes (30/60/15 min dawn, light-only, backup), morning only, same sound on every Mac in sync, auto-stop |
| **Menu bar app** (`app/`) | Brightness, white/color, scenes, curve editor, alarm editor, sound sync calibration |
| **Phone page** (`/app`) | Big Stop/Snooze when the alarm rings, light controls; add it to the iPhone Home Screen |
| **MCP connector** (`tinylight.mcp`) | 16 tools for any agent (Claude, Codex, …), stdio or HTTP (`POST /mcp`), rules built in |
| **Apple Home** (`tinylight.homekit`) | "Room light" lightbulb + "Light schedule" and "Alarm" switches; Siri and Shortcuts |
| **Helper** (`tinylight`) | `scan`, `connect`, `probe`, `new-driver`, `try`, `install`, `doctor` |

## Quick start (macOS)

```bash
git clone https://github.com/edwin0912-00/tinyLight && cd tinyLight
python3 -m venv .venv && .venv/bin/pip install -e '.[homekit]'
.venv/bin/tinylight connect     # run in Terminal.app: it needs Bluetooth permission
.venv/bin/tinylight install     # bridge + Apple Home at login
```

Then:

- Agents: `claude mcp add -s user room-light -- $PWD/.venv/bin/python -m tinylight.mcp`
  (from another machine: `--transport http http://<bridge>.local:8787/mcp --header "Authorization: Bearer <token>"`).
- Phone: open `http://<bridge>.local:8787/app?token=<token>` and *Add to Home Screen*. The token is in `~/.tinylight/token`.
- Apple Home: *Add Accessory → More options → Room light*, code in `~/.tinylight/homekit/PAIRING.txt`.
- Menu bar app: `app/build.sh`, then copy `app/build/tinyLight.app` to `~/Applications`.
  On another Mac put `{"url": "http://<bridge>.local:8787", "token": "<token>"}` into
  `~/Library/Application Support/tinyLight/config.json`.

## Connect a new light

`tinylight connect` scans, fingerprints each device against the drivers and pairs a known one.
For an unknown light it saves the GATT table and points to the next steps:
[docs/connect-any-light.md](docs/connect-any-light.md). The Nanlite story, with every dead end, is in
[docs/nanlite-mesh.md](docs/nanlite-mesh.md).

## Things we measured the hard way

- macOS gives Bluetooth only to Terminal.app or a LaunchAgent with permission; over SSH bleak fails.
- A light that holds a connection stops advertising, so scans do not see it while the bridge runs.
- HTTP keep-alive on the bridge cut clock-sync error from about ±70 ms to about ±8 ms over Wi-Fi.
- `AVAudioEngine` hung for >6 s on a Mac whose default output was a virtual device; the app plays with
  `AVAudioPlayer` pinned to the built-in speakers instead.
- Ad-hoc signed apps lose microphone/Bluetooth permission on every rebuild; sign with a stable local identity
  (`SIGN_ID=... app/build.sh`).

## Safety

The bridge listens on your LAN. Requests from other hosts need the token; `/raw` (protocol probes) is
localhost only. Keys, token and state live in `$TINYLIGHT_HOME` and are git-ignored.

## License

MIT. Nanlite driver code derives from nicholasmullikin/nanlite (MIT); credits for protocol notes, the
TEA key and the CC0 sounds are in [THIRD_PARTY.md](THIRD_PARTY.md). Not affiliated with Nanlite.
