"""Validate generic release metadata without packaging private development files."""

import json
from pathlib import Path
from zipfile import ZipFile

root = Path(__file__).resolve().parents[1]
component = root / "custom_components" / "atvv_remote"
manifest = json.loads((component / "manifest.json").read_text())
assert manifest["domain"] == component.name
assert manifest["config_flow"]
assert manifest["version"]
assert (root / "LICENSE").is_file()
assert (root / "THIRD_PARTY_NOTICES.md").is_file()
assert json.loads((component / "strings.json").read_text()) == json.loads(
    (component / "translations" / "en.json").read_text()
)
json.loads((root / "hacs.json").read_text())
# Optional release artifact lives outside the component and is ignored by Git.
dist = root / "dist"
dist.mkdir(exist_ok=True)
with ZipFile(dist / "atvv_remote.zip", "w") as archive:
    for path in sorted(component.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".json") and "__pycache__" not in path.parts:
            archive.write(path, path.relative_to(component))
print("Metadata validated; component-only release archive built")
