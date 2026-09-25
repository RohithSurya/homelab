"""Download backends for bridge.py. See base.py for the contract.

Backends are imported lazily, so one whose env isn't configured (e.g. no SEEDR_EMAIL) is
only an error if a route or a live state file actually names it."""

import importlib

# route name -> "module:Class"
REGISTRY = {
    "seedr": "backends.seedr:SeedrBackend",
}

_instances = {}


def get(name):
    """The backend instance for name. Raises KeyError for an unknown name, and whatever the
    backend's import raises for a known but unconfigured one."""
    if name not in _instances:
        mod, cls = REGISTRY[name].split(":")
        _instances[name] = getattr(importlib.import_module(mod), cls)()
    return _instances[name]
