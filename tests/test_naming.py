"""The product is Bluetooth Remote Voice; ATVV names only one of the protocols."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "bt_remote_voice"
OLD = ("atvv_remote", "ATVV Remote Voice", "ha-atvv-remote")


def test_component_folder_matches_domain():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["domain"] == COMPONENT.name == "bt_remote_voice"
    assert manifest["name"] == "Bluetooth Remote Voice"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


def test_no_old_product_name_in_tracked_text():
    skip = {".git", "dist", ".pytest_cache", ".ruff_cache"}
    for path in ROOT.rglob("*"):
        if path.is_dir() or skip & set(path.relative_to(ROOT).parts):
            continue
        if path.suffix not in {".py", ".json", ".md", ".svg", ".sh", ".yml", ".txt", ""}:
            continue
        if path.name == "test_naming.py":
            continue
        text = path.read_text(errors="ignore")
        for old in OLD:
            assert old not in text, f"{old!r} still in {path.relative_to(ROOT)}"


def test_background_task_names_are_product_neutral():
    for path in COMPONENT.glob("*.py"):
        for line in path.read_text().splitlines():
            if "name=" in line or "create_task(" in line:
                assert '"ATVV ' not in line, f"{path.name}: {line.strip()}"
