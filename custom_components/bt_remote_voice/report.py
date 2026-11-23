"""The shareable Remote Report: what a maintainer needs to classify a remote.

It is built from data the integration already holds (never new device reads), is total (it never
raises, missing data reads ``unknown``), is bounded in size, and masks every Bluetooth address
form down to the public manufacturer prefix. It is text for a GitHub issue, not UI copy, so it
is English only; the screens that show it are translated.
"""

import hashlib
import json
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path

from homeassistant.const import __version__ as CORE_VERSION

from .hid_descriptor import find_voice_reports, voice_checks
from .preferences import PIPELINE_PREFERRED

SCHEMA = 1
MAX_CHARS = 19_000
UNKNOWN = "unknown"

try:  # read once at import (module import runs off the event loop)
    INTEGRATION_VERSION = json.loads(Path(__file__).with_name("manifest.json").read_text())[
        "version"
    ]
except OSError, ValueError, KeyError:
    INTEGRATION_VERSION = UNKNOWN


def versions():
    """``integration``/``core``/``python`` fields for ReportInput."""
    return {
        "integration": INTEGRATION_VERSION,
        "core": CORE_VERSION,
        "python": platform.python_version(),
    }


# Six hex octets joined by ":", "-" or "_" in any mix, with no boundary requirement, so addresses
# glued to text or to each other (and BlueZ object paths such as dev_AA_BB_...) are masked too.
# Over-masking is harmless; a leaked address is not.
_ADDRESS = re.compile(r"[0-9A-Fa-f]{2}(?:[:_-][0-9A-Fa-f]{2}){5}")


def _prefix(match):
    octets = re.split(r"[:_-]", match.group(0))
    return ":".join(octets[:3]).upper() + ":xx:xx:xx"


def mask_address(text, known=()):
    """Hide the device part of every Bluetooth address; keep the public manufacturer prefix.

    ``known`` are addresses the caller knows are in play: they are also masked in their
    separator-less 12-digit form, which the generic pattern cannot recognise.
    """
    for address in known:
        digits = re.sub(r"[^0-9A-Fa-f]", "", str(address or ""))
        if len(digits) == 12:
            pairs = [digits[i : i + 2] for i in range(0, 12, 2)]
            glue = r"[\s.:_-]?"  # the same bytes with any (or no) separator, either order
            prefix = ":".join(pairs[:3]).upper()
            text = re.sub(glue.join(pairs), f"{prefix}:xx:xx:xx", text, flags=re.I)
            text = re.sub(glue.join(reversed(pairs)), "xxxxxxxxxxxx", text, flags=re.I)
    return _ADDRESS.sub(_prefix, text)


@dataclass
class ReportInput:
    integration: str = UNKNOWN
    core: str = UNKNOWN
    python: str = UNKNOWN
    adapter: dict = field(default_factory=dict)
    remote: dict = field(default_factory=dict)  # Remote.signals() plus remote_type/detected
    hid: dict = field(default_factory=dict)  # hid_summary(...)
    config: dict = field(default_factory=dict)  # config_summary(...)
    observation: dict = field(default_factory=dict)  # Observation.to_dict() from the Test step
    outcome: dict = field(default_factory=dict)  # {"error": code|None, "stage": str}


def hid_summary(reports, descriptor_hex, nodes=None):
    """HID facts for the report from a parsed descriptor (see hid_descriptor.parse_reports)."""
    reports = tuple(reports or ())
    return {
        "nodes": nodes,
        "descriptor_hex": descriptor_hex,
        "reports": [
            {
                "id": r.id,
                "kind": r.kind,
                "size_bytes": r.size_bytes,
                "usage_page": None if r.usage_page is None else f"0x{r.usage_page:04x}",
                "is_array": r.is_array,
            }
            for r in reports
        ],
        "voice_reports": bool(find_voice_reports(reports)) if reports else None,
        "voice_checks": voice_checks(reports) if reports else None,
        "descriptor_sha256": _sha(descriptor_hex),
    }


def _sha(descriptor_hex):
    """Short fingerprint of the descriptor so reports of the same remote model match at a glance."""
    try:
        return hashlib.sha256(bytes.fromhex(descriptor_hex)).hexdigest()[:16]
    except TypeError, ValueError:
        return None


def config_summary(options, data):
    """The settings that matter for support, with no names, entity ids or conditions."""
    options, data = options or {}, data or {}
    pipeline = options.get("pipeline_id", data.get("pipeline_id"))
    return {
        "remote_type": options.get("remote_type", "auto"),
        "mode": options.get("mode", data.get("mode", UNKNOWN)),
        "input_mode": options.get("input_mode", "single"),
        "min_press_seconds": options.get("min_press_seconds", 0),
        "pipeline": (
            UNKNOWN
            if not pipeline
            else "preferred"
            if pipeline == PIPELINE_PREFERRED
            else "explicit"
        ),
        "speaker": "set" if options.get("speaker") else "none",
        "reply_condition": "set" if options.get("reply_condition") else "none",
    }


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value, limit):
    return list(value)[:limit] if isinstance(value, (list, tuple)) else []


def _clip(value, limit=200):
    return value if not isinstance(value, str) else value[:limit]


def _payload(data, *, keep_descriptor):
    remote, hid = _dict(data.remote), _dict(data.hid)
    descriptor = hid.get("descriptor_hex")
    return {
        "schema": SCHEMA,
        "integration": _clip(data.integration),
        "core": _clip(data.core),
        "python": _clip(data.python),
        "adapter": {k: _clip(v) for k, v in _dict(data.adapter).items()},
        "remote": {
            "address": _clip(remote.get("address", UNKNOWN)),
            "family": remote.get("family", UNKNOWN),
            "reasons": _list(remote.get("reasons"), 8),
            "vendor_id": remote.get("vendor_id", UNKNOWN),
            "product_id": remote.get("product_id", UNKNOWN),
            "modalias": _clip(remote.get("modalias", UNKNOWN)),
            "uuids": [_clip(u, 40) for u in _list(remote.get("uuids"), 32)],
            "manufacturer": [
                {"company": _clip(m.get("company"), 8), "value": _clip(m.get("value"), 64)}
                for m in _list(remote.get("manufacturer"), 16)
                if isinstance(m, dict)
            ],
            "appearance": remote.get("appearance", UNKNOWN),
            "bonded": remote.get("bonded", UNKNOWN),
            "connected": remote.get("connected", UNKNOWN),
            "remote_type": remote.get("remote_type", UNKNOWN),
        },
        "hid": {
            "nodes": hid.get("nodes", UNKNOWN),
            "voice_reports": hid.get("voice_reports", UNKNOWN),
            "voice_checks": hid.get("voice_checks") or UNKNOWN,
            "descriptor_sha256": hid.get("descriptor_sha256") or UNKNOWN,
            "reports": [r for r in _list(hid.get("reports"), 64) if isinstance(r, dict)],
            "descriptor_hex": (
                _clip(descriptor, 4096)
                if keep_descriptor and isinstance(descriptor, str)
                else UNKNOWN
            ),
            "descriptor_truncated": isinstance(descriptor, str) and len(descriptor) > 4096,
        },
        "observation": _dict(data.observation) or UNKNOWN,
        "config": {k: _clip(v) for k, v in _dict(data.config).items()},
        "outcome": {k: _clip(v) for k, v in _dict(data.outcome).items()},
    }


def _checks_line(checks):
    if not isinstance(checks, list):
        return UNKNOWN
    failed = [f"{c.get('check')} (found {c.get('found')})" for c in checks if not c.get("ok")]
    return "all passed" if not failed else "failed: " + ", ".join(failed)


def _render(payload):
    known = (payload["remote"]["address"], *(a for a in payload["adapter"].values()))
    remote, hid, outcome = payload["remote"], payload["hid"], payload["outcome"]
    lines = [
        "# Bluetooth Remote Voice - remote report",
        "",
        f"Integration {payload['integration']} | Home Assistant {payload['core']} "
        f"| Python {payload['python']}",
        "",
        f"- Remote: {remote['address']}, detected as **{remote['family']}** "
        f"(reasons: {', '.join(remote['reasons']) or 'none'})",
        f"- Vendor / product: {remote['vendor_id']} / {remote['product_id']}",
        f"- HID voice reports (0xF0 audio, 0xF2 mic): {hid['voice_reports']}",
        f"- Voice checks: {_checks_line(hid['voice_checks'])}",
        f"- Remote type setting: {remote['remote_type']}",
        f"- Result: {outcome.get('error', UNKNOWN)} ({outcome.get('stage', UNKNOWN)})",
        "",
        "Paste this into a new GitHub issue. It has no full Bluetooth address, names, URLs or audio.",
        "",
        "```json",
        json.dumps(payload, indent=2, default=str),
        "```",
    ]
    return mask_address("\n".join(lines), known)


def build_report(data):
    """The report text (Markdown summary plus one JSON block); never raises, always bounded."""
    try:
        text = _render(_payload(data, keep_descriptor=True))
        if len(text) > MAX_CHARS:
            text = _render(_payload(data, keep_descriptor=False))
        if len(text) > MAX_CHARS:
            text = _render(_payload(ReportInput(), keep_descriptor=False))
    except Exception:  # noqa: BLE001 - a report must never break setup or diagnostics
        text = _render(_payload(ReportInput(), keep_descriptor=False))
    return text
