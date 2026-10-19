"""Which media-bridge components run (MEDIA_BRIDGE_COMPONENTS). Kept apart from main.py so it
imports nothing that needs the services' env."""

from common import clog

COMPONENTS = ["bridge", "reaper", "movierulz", "tamilmv", "youtube"]


def enabled_components(spec):
    """MEDIA_BRIDGE_COMPONENTS: comma-separated subset of COMPONENTS, default all.
    Dev runs without 'bridge' and 'reaper' so a dev copy can never touch the real
    seedbox/Seedr or delete movies. Unknown names are logged and ignored."""
    if not spec or not spec.strip():
        return list(COMPONENTS)
    want = [p.strip() for p in spec.split(",") if p.strip()]
    for p in want:
        if p not in COMPONENTS:
            clog("main", f"ignoring unknown component '{p}' (known: {', '.join(COMPONENTS)})")
    return [c for c in COMPONENTS if c in want]
