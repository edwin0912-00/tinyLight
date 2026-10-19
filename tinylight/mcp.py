"""MCP server "room-light": the Nanlite light in the owner's room, via the nanlited bridge.

Two ways in (stdlib only):
  stdio on the bridge Mac:  python3 -m tinylight.mcp
  HTTP anywhere:    POST http://<bridge-host>.local:8787/mcp  (Authorization: Bearer <token>)
NANLITE_URL overrides the bridge address (default http://127.0.0.1:8787).
Rules for agents live in AGENT-RULES.md next to this file and ship as MCP instructions.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("NANLITE_URL", "http://127.0.0.1:8787")
HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(HERE, "AGENT-RULES.md")) as f:
        INSTRUCTIONS = f.read()
except OSError:
    INSTRUCTIONS = "Room light via the tinyLight bridge. Change it only when the owner asks."

NO_ARGS = {"type": "object", "properties": {}}
TOOLS = [
    {"name": "light_set", "title": "Room light: white",
     "description": "Set the room light to white light. Any subset of: brightness 0-100 (%), "
                    "cct 2700-6500 (Kelvin, warm to cold), gm -50..50 (tint, 0 neutral, - green, + magenta). "
                    "brightness 0 turns it off. Switches from color mode back to white.",
     "inputSchema": {"type": "object", "properties": {
         "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
         "cct": {"type": "integer", "minimum": 2700, "maximum": 6500},
         "gm": {"type": "integer", "minimum": -50, "maximum": 50}}}},
    {"name": "light_color", "title": "Room light: color",
     "description": "Set the room light to a color. hue 0-360 degrees (0 red, 30 orange, 55 yellow, "
                    "120 green, 190 cyan, 240 blue, 285 violet, 320 pink), sat 0-100 %, optional brightness 0-100.",
     "inputSchema": {"type": "object", "properties": {
         "hue": {"type": "integer", "minimum": 0, "maximum": 360},
         "sat": {"type": "integer", "minimum": 0, "maximum": 100},
         "brightness": {"type": "integer", "minimum": 0, "maximum": 100}}}},
    {"name": "light_on", "title": "Room light: on",
     "description": "Turn the room light on at its last brightness.", "inputSchema": NO_ARGS},
    {"name": "light_off", "title": "Room light: off",
     "description": "Turn the room light off.", "inputSchema": NO_ARGS},
    {"name": "light_state", "title": "Room light: state",
     "description": "Last values sent to the room light (mode, brightness, cct, gm, hue, sat). "
                    "The light cannot report its real state.", "inputSchema": NO_ARGS},
    {"name": "scene_list", "title": "Room light: list scenes",
     "description": "Saved scenes of the room light with their values.", "inputSchema": NO_ARGS},
    {"name": "scene_apply", "title": "Room light: apply scene",
     "description": "Apply a saved scene by name (see scene_list).",
     "inputSchema": {"type": "object", "required": ["name"],
                     "properties": {"name": {"type": "string"}}}},
    {"name": "scene_save", "title": "Room light: save scene",
     "description": "Save the current light setting as a named scene. Only when the owner asks.",
     "inputSchema": {"type": "object", "required": ["name"],
                     "properties": {"name": {"type": "string"}}}},
    {"name": "schedule_get", "title": "Room light: daily schedule",
     "description": "The daily light curve: enabled, keyframes (time or sunset/sunrise + offset min, "
                    "level 0-100, kelvin, ease_in/ease_out influence %, hold), today's resolved times, sunset, current target "
                    "and paused_until (a manual change pauses it until the next keyframe).",
     "inputSchema": NO_ARGS},
    {"name": "schedule_set", "title": "Room light: replace schedule points",
     "description": "Replace the daily curve. points: list of {time 'HH:MM'} or {sun 'sunset'|'sunrise', "
                    "offset minutes}, plus level 0-100 (0 = off), kelvin 2700-6500, and After Effects style easing: "
                    "ease_in / ease_out = influence 0-100 % arriving at / leaving the point (0 = linear, "
                    "50 = soft, 90 = very soft), hold true keeps the value until the next point. Only when the owner asks. Read schedule_get first and keep "
                    "points the owner did not mention.",
     "inputSchema": {"type": "object", "required": ["points"], "properties": {
         "points": {"type": "array", "minItems": 2, "items": {"type": "object", "properties": {
             "time": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
             "sun": {"type": "string", "enum": ["sunset", "sunrise"]},
             "offset": {"type": "integer"},
             "level": {"type": "integer", "minimum": 0, "maximum": 100},
             "kelvin": {"type": "integer", "minimum": 2700, "maximum": 6500},
             "ease_in": {"type": "integer", "minimum": 0, "maximum": 100},
             "ease_out": {"type": "integer", "minimum": 0, "maximum": 100},
             "hold": {"type": "boolean"}},
             "required": ["level", "kelvin"]}}}}},
    {"name": "schedule_enable", "title": "Room light: schedule on/off",
     "description": "Turn the daily schedule on or off.",
     "inputSchema": {"type": "object", "required": ["on"], "properties": {"on": {"type": "boolean"}}}},
    {"name": "alarm_list", "title": "Room light: alarms",
     "description": "Alarms (time, days 0=Mon, mode, sound, enabled), next occurrences, modes with light and "
                    "sound curves (t = minutes from alarm time), available sounds, and the active alarm state.",
     "inputSchema": NO_ARGS},
    {"name": "alarm_set", "title": "Room light: replace alarms",
     "description": "Replace the alarm list. Morning only (04:00-11:59). Each alarm: id, time 'HH:MM', days [0..6], "
                    "mode (gentle|long|quick|light|backup or a custom mode), sound (see alarm_list), enabled. "
                    "Only when the owner asks. Read alarm_list first and keep alarms the owner did not mention.",
     "inputSchema": {"type": "object", "required": ["alarms"], "properties": {"alarms": {"type": "array", "items": {
         "type": "object", "required": ["id", "time", "mode"], "properties": {
             "id": {"type": "string"}, "time": {"type": "string", "pattern": "^[0-1][0-9]:[0-5][0-9]$"},
             "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
             "mode": {"type": "string"}, "sound": {"type": "string"}, "enabled": {"type": "boolean"}}}}}}},
    {"name": "alarm_stop", "title": "Room light: stop alarm",
     "description": "Stop the running alarm on every device at once.", "inputSchema": NO_ARGS},
    {"name": "alarm_snooze", "title": "Room light: snooze alarm",
     "description": "Snooze the running alarm for 9 minutes (sound stops, light stays).", "inputSchema": NO_ARGS},
    {"name": "schedule_resume", "title": "Room light: resume schedule",
     "description": "End a manual pause now; the light moves back onto the curve.", "inputSchema": NO_ARGS},
]
PATHS = {"light_on": "/on", "light_off": "/off", "light_state": "/state", "scene_list": "/scenes",
         "schedule_get": "/schedule", "schedule_resume": "/schedule/resume",
         "alarm_list": "/alarms", "alarm_stop": "/alarm/stop", "alarm_snooze": "/alarm/snooze"}


def call_bridge(path: str, body: dict | None = None) -> str:
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + path, data=data), timeout=45) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()
    except OSError as e:
        return json.dumps({"error": f"bridge unreachable at {URL}: {e}"})


def tool_path(name: str, args: dict) -> str | None:
    if name in ("light_set", "light_color"):
        keys = ("brightness", "cct", "gm") if name == "light_set" else ("hue", "sat", "brightness")
        return "/set?" + urllib.parse.urlencode({k: args[k] for k in keys if k in args})
    if name == "schedule_enable":
        return "/schedule/enable?on=" + ("1" if args.get("on") else "0")
    if name in ("scene_apply", "scene_save"):
        path = "/scene" if name == "scene_apply" else "/scene/save"
        return path + "?" + urllib.parse.urlencode({"name": args.get("name", "")})
    return PATHS.get(name)


def handle(msg: dict):
    m, mid = msg.get("method"), msg.get("id")
    if m == "initialize":
        res = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
               "capabilities": {"tools": {}},
               "serverInfo": {"name": "room-light", "title": "Room light (tinyLight bridge)",
                              "version": "0.6.0"},
               "instructions": INSTRUCTIONS}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    elif m == "tools/call":
        name = msg["params"]["name"]
        path = tool_path(name, msg["params"].get("arguments") or {})
        if path is None and name not in ("schedule_set", "alarm_set"):
            res = {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        else:
            args = msg["params"].get("arguments") or {}
            out = (call_bridge("/schedule", {"points": args.get("points")}) if name == "schedule_set"
                   else call_bridge("/alarms", {"alarms": args.get("alarms")}) if name == "alarm_set"
                   else call_bridge(path))
            res = {"content": [{"type": "text", "text": out}], "isError": '"error"' in out}
    elif m == "ping":
        res = {}
    elif mid is None:
        return None  # notification
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {m}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
