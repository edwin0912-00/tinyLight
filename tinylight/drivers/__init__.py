"""Driver registry. Add a light family: create drivers/<name>.py (or a package) with a Driver
subclass and list it here. Start from drivers/template.py."""

from __future__ import annotations

from importlib import import_module

DRIVERS = {
    "nanlite_mesh": "tinylight.drivers.nanlite_mesh:NanliteMeshDriver",
    "dummy": "tinylight.drivers.dummy:DummyDriver",
}


def driver_class(name: str):
    mod, cls = DRIVERS[name].split(":")
    return getattr(import_module(mod), cls)


def load(name: str):
    return driver_class(name)()


def all_classes():
    out = []
    for n in DRIVERS:
        try:
            out.append(driver_class(n))
        except Exception as e:  # a driver with missing deps must not break discovery
            print(f"driver {n} unavailable: {e}")
    return out
