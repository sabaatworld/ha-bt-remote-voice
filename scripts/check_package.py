"""Validate generic release metadata without packaging private development files."""

import json
import os
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
component = root / "custom_components" / "bt_remote_voice"
manifest = json.loads((component / "manifest.json").read_text())
assert manifest["domain"] == component.name
assert manifest["config_flow"]
assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]), "version must be SemVer"
# A release tag (for example v0.4.0) must equal the manifest version.
tag = os.environ.get("GITHUB_REF_NAME", "")
if tag.startswith("v"):
    assert tag == f"v{manifest['version']}", f"tag {tag} != manifest version {manifest['version']}"
assert (root / "LICENSE").is_file()
assert (component / "THIRD_PARTY_NOTICES.md").is_file(), "notice must ship inside the component"
hacs = json.loads((root / "hacs.json").read_text())
assert hacs["filename"] == f"{component.name}.zip"
assert not (component / "strings.json").exists(), "translations/en.json is the only source"
categories = manifest.get("categories")
assert isinstance(categories, list) and 1 <= len(categories) <= 3, "1-3 categories required"
assert (component / "translations" / "en.json").is_file()
# Optional release artifact lives outside the component and is ignored by Git.
dist = root / "dist"
dist.mkdir(exist_ok=True)
with ZipFile(dist / "bt_remote_voice.zip", "w", ZIP_DEFLATED) as archive:
    for path in sorted(component.rglob("*")):
        is_source = path.suffix in (".py", ".json")
        is_notice = path == component / "THIRD_PARTY_NOTICES.md"
        is_brand = path.parent == component / "brand" and path.suffix == ".png"
        is_shipped = is_source or is_brand or is_notice
        if path.is_file() and is_shipped and "__pycache__" not in path.parts:
            archive.write(path, path.relative_to(component))
with ZipFile(dist / "bt_remote_voice.zip") as archive:
    assert "THIRD_PARTY_NOTICES.md" in archive.namelist(), "notice missing from release zip"
print("Metadata validated; component-only release archive built")
