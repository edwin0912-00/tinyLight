"""Where tinyLight keeps its data. Nothing here is ever committed to git.

TINYLIGHT_HOME (default ~/.tinylight): config.json, keys, token, state, scenes, schedule, alarms, devices.
TINYLIGHT_SOUNDS (default <repo>/sounds): alarm sounds (.m4a).
"""

import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser(os.environ.get("TINYLIGHT_HOME", "~/.tinylight"))
SOUNDS = os.path.expanduser(os.environ.get("TINYLIGHT_SOUNDS", os.path.join(REPO, "sounds")))
os.makedirs(HOME, exist_ok=True)


def data(name: str) -> str:
    return os.path.join(HOME, name)


def config() -> dict:
    """config.json: {"driver": "nanlite_mesh", "port": 8787, "lat": .., "lon": .., "name": "Room light"}"""
    try:
        with open(data("config.json")) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(c: dict):
    with open(data("config.json"), "w") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)
