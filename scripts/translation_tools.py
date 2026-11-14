"""Helpers and checks for the integration's translation files.

`translations/en.json` is the only source. Every other Home Assistant language has a file with
exactly the same keys, placeholders and Markdown markers. Usage:

    python scripts/translation_tools.py status           # coverage and problems per language
    python scripts/translation_tools.py check <lang>     # problems for one language
    python scripts/translation_tools.py refresh-hashes   # record the English text as translated
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSLATIONS = ROOT / "custom_components" / "bt_remote_voice" / "translations"
HASHES = Path(__file__).resolve().parent / "translation_sources.json"
PLACEHOLDER = re.compile(r"\{[^{}]+\}")
MARKERS = ("**", "`", "\n")
# Values that may legitimately be identical to the English text in every language.
SAME_AS_ENGLISH = re.compile(r"^[\W\d_]*$")  # no letters at all
# Real cognates that several languages spell the same way as English.
ALLOWED_SAME_WORDS = {"Code", "Message", "Sessions"}
# Variants of English may match it, and the integration title is the product name everywhere.
ENGLISH_VARIANTS = {"en-GB"}
PRODUCT_NAME_KEYS = {"title"}
PRODUCT_NAME = "Bluetooth Remote Voice"


def load(lang: str) -> dict:
    return json.loads((TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))


def flatten(node: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def placeholders(value: str) -> list[str]:
    return sorted(PLACEHOLDER.findall(value))


def markers(value: str) -> dict[str, int]:
    return {marker: value.count(marker) for marker in MARKERS}


def source_hashes(english: dict) -> dict[str, str]:
    return {
        key: hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        for key, value in sorted(flatten(english).items())
    }


def problems(lang: str) -> list[str]:
    english = flatten(load("en"))
    try:
        other = flatten(load(lang))
    except FileNotFoundError:
        return [f"{lang}: file missing"]
    found: list[str] = []
    for key in sorted(set(english) - set(other)):
        found.append(f"{lang}: missing key {key}")
    for key in sorted(set(other) - set(english)):
        found.append(f"{lang}: extra key {key}")
    for key in sorted(set(english) & set(other)):
        source, text = english[key], other[key]
        if not isinstance(text, str) or not text.strip():
            found.append(f"{lang}: empty value {key}")
            continue
        if placeholders(source) != placeholders(text):
            found.append(f"{lang}: placeholders differ in {key}")
        if markers(source) != markers(text):
            found.append(f"{lang}: markdown/newline markers differ in {key}")
        if key in PRODUCT_NAME_KEYS and text != PRODUCT_NAME:
            found.append(f"{lang}: {key} must stay {PRODUCT_NAME!r}")
        elif (
            text == source
            and lang not in ENGLISH_VARIANTS
            and key not in PRODUCT_NAME_KEYS
            and source not in ALLOWED_SAME_WORDS
            and not SAME_AS_ENGLISH.match(source)
        ):
            found.append(f"{lang}: untranslated {key}")
    return found


def languages() -> list[str]:
    return sorted(p.stem for p in TRANSLATIONS.glob("*.json"))


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    if command == "refresh-hashes":
        HASHES.write_text(
            json.dumps(source_hashes(load("en")), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Recorded {len(source_hashes(load('en')))} English values")
        return 0
    langs = [a for a in argv[2:]] if command == "check" else [x for x in languages() if x != "en"]
    bad = [line for lang in langs for line in problems(lang)]
    print("\n".join(bad) if bad else f"{len(langs)} language file(s) clean")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
