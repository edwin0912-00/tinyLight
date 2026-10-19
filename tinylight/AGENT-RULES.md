# Room light (tinyLight) - rules for agents

A Bluetooth light in the owner's room. One Mac (the bridge) owns the Bluetooth link; every client goes
through the bridge on port 8787.

## Connect

- MCP, stdio on the bridge Mac: `python3 -m tinylight.mcp` (server `room-light`).
- MCP over HTTP from any machine that reaches the bridge: `http://<bridge-host>.local:8787/mcp`,
  header `Authorization: Bearer <token>`. The token is in `$TINYLIGHT_HOME/token`. Never print or log it.
- Plain HTTP: `GET /state /on /off /toggle /set /scenes /scene?name=`.
- People: the menu bar app, the phone page `/app`, the Apple Home accessory "Room light".

## Modes

- White: brightness 1-100 %, temperature in the driver's range (often 2700-6500 K),
  tint -50..+50 (0 = neutral, minus = green, plus = magenta).
- Color (if the light supports it): hue 0-360 (0 red, 30 orange, 55 yellow, 120 green, 190 cyan,
  240 blue, 285 violet, 320 pink), saturation 0-100 %.
- Brightness 0 or `light_off` turns the light off. `light_on` restores the last brightness.

## Daily schedule

- The bridge follows a 24 h curve: keyframes with level, warmth and After Effects style easing
  (ease_in / ease_out influence 0-100 %, bezier between points; hold = keep value until the next point).
  Points can follow sunset or sunrise with an offset (location from config.json).
- A manual change (any client) pauses the schedule until the next keyframe. `schedule_resume` ends the pause.
- Edit the curve in the menu bar app (Schedule tab) or with `schedule_get` then `schedule_set`.
  Keep points the owner did not ask to change.

## Alarms

- Sunrise alarms, morning only (04:00-11:59; the light never starts before 03:30). Modes: gentle (30 min
  dawn, sound from -3 min), long (60 min), quick (15 min), light (no sound), backup (louder chimes at +10).
- The same sound plays on every Mac running the menu bar app, in sync; stop or snooze from any device
  (app, phone page, Apple Home, MCP) stops all. The alarm stops by itself `ring_minutes` (default 5) after it starts.
- Tools: alarm_list, alarm_set, alarm_stop, alarm_snooze. Change alarms only when the owner asks.

## Rules

1. Change the light only when the owner asks, or when a routine the owner set up asks.
2. Prefer saved scenes (`scene_list`, `scene_apply`) over raw values when the request matches one.
3. Do not mix modes in one call: white uses cct/gm, color uses hue/sat.
4. The light cannot report its real state. `light_state` returns the last values sent.
5. Do not probe the protocol or send raw packets. The owner may be in the room.
6. Never re-pair (provision) the light from another machine. The bridge holds the only keys.
7. If the bridge answers with an error, report it. Do not retry in a loop.
