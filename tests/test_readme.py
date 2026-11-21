"""The README guide must not drift from the integration it describes."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text()
EN = json.loads((ROOT / "custom_components/bt_remote_voice/translations/en.json").read_text())


def slug(heading):
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def test_every_internal_anchor_resolves():
    anchors = {slug(h) for h in re.findall(r"^#{1,6} (.+)$", README, re.M)}
    missing = [a for a in re.findall(r"\]\(#([^)]+)\)", README) if a not in anchors]
    assert not missing, missing


def _values(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _values(value)
    elif isinstance(node, str):
        yield node


def test_ui_labels_named_in_the_guide_exist_in_english_strings():
    labels = set(_values(EN))
    for label in ("Remote type", "Show remote report", "Automatic (recommended)"):
        assert label in labels, label
    for label in re.findall(r"\*\*(Remote type|Show remote report)\*\*", README):
        assert label in labels


def test_readme_report_field_list_matches_what_the_report_emits():
    from custom_components.bt_remote_voice.report import ReportInput, build_report

    block = json.loads(
        re.search(r"```json\n(.*?)\n```", build_report(ReportInput()), re.S).group(1)
    )
    line = next(x for x in README.splitlines() if x.startswith("The report has these fields:"))
    assert set(re.findall(r"`(\w+)`", line)) == set(block) - {"schema"}


def test_issue_template_path_the_guide_links_exists():
    assert (ROOT / ".github/ISSUE_TEMPLATE/new-remote.yml").exists()
    assert "template=new-remote.yml" in README


def test_readme_capture_commands_are_exactly_the_script():
    script = (ROOT / "scripts/capture_remote_info.sh").read_text()
    body = "\n".join(line for line in script.splitlines() if not line.startswith("#!")).strip()
    block = re.search(r"```sh\n(# Read-only helper.*?)\n```", README, re.S).group(1).strip()
    assert block == body
    # The script leaves out the remote's own name and alias (the report promises "no names").
    assert "Name|Alias" in script and "HID_NAME" in script
