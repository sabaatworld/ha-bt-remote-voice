"""Remote type: which protocol family to speak to a paired remote.

The choice is a behavior, never a model: a product id does not determine how a remote's
microphone works, and clones reuse ids and names. ``auto`` trusts the classification recorded at
setup (``entry.data["family"]``); a forced type is an explicit user override.
"""

REMOTE_TYPES = ("auto", "atvv", "fire_press_to_talk")
DEFAULT_REMOTE_TYPE = "auto"


def transport_family(options, data):
    """``"fire"`` or ``"atvv"``: the forced remote type, else the family recorded at setup."""
    forced = (options or {}).get("remote_type", DEFAULT_REMOTE_TYPE)
    if forced == "fire_press_to_talk":
        return "fire"
    if forced == "atvv":
        return "atvv"
    return "fire" if (data or {}).get("family") == "fire" else "atvv"
