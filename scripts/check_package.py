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
assert (root / "THIRD_PARTY_NOTICES.md").is_file()
hacs = json.loads((root / "hacs.json").read_text())
assert hacs["filename"] == f"{component.name}.zip"
assert json.loads((component / "strings.json").read_text()) == json.loads(
    (component / "translations" / "en.json").read_text()
)
# Optional release artifact lives outside the component and is ignored by Git.
dist = root / "dist"
dist.mkdir(exist_ok=True)
with ZipFile(dist / "bt_remote_voice.zip", "w", ZIP_DEFLATED) as archive:
    for path in sorted(component.rglob("*")):
        is_source = path.suffix in (".py", ".json")
        is_brand = path.parent == component / "brand" and path.suffix == ".png"
        if path.is_file() and (is_source or is_brand) and "__pycache__" not in path.parts:
            archive.write(path, path.relative_to(component))
print("Metadata validated; component-only release archive built")
