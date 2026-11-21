"""Release tooling: beta versions are accepted, mismatched tags fail, betas are published as pre-releases."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_package.py"
WORKFLOW = (ROOT / ".github/workflows/release.yml").read_text()


def check(tag=None):
    env = {**os.environ}
    env.pop("GITHUB_REF_NAME", None)
    if tag:
        env["GITHUB_REF_NAME"] = tag
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def manifest_version():
    return json.loads((ROOT / "custom_components/bt_remote_voice/manifest.json").read_text())[
        "version"
    ]


def test_the_current_manifest_version_passes_and_the_matching_tag_passes():
    assert check().returncode == 0
    assert check(f"v{manifest_version()}").returncode == 0


def test_a_tag_that_differs_from_the_manifest_version_fails():
    result = check("v9.9.9")
    assert result.returncode != 0 and "tag v9.9.9" in result.stderr


def test_the_version_pattern_accepts_beta_and_rc_but_not_other_suffixes():
    source = SCRIPT.read_text()
    pattern = re.search(r're\.fullmatch\(r"(.+?)", manifest\["version"\]\)', source).group(1)
    for good in ("0.6.0", "0.6.0-beta.1", "1.0.0-rc.12"):
        assert re.fullmatch(pattern, good), good
    for bad in ("0.6", "0.6.0-beta", "0.6.0b1", "0.6.0-alpha.1", "v0.6.0", "0.6.0-beta.1.2"):
        assert not re.fullmatch(pattern, bad), bad


def test_home_assistants_own_parser_reads_the_beta_version_in_the_right_order():
    from awesomeversion import AwesomeVersion

    assert AwesomeVersion("0.6.0-beta.1") > AwesomeVersion("0.5.0")
    assert AwesomeVersion("0.6.0-beta.1") < AwesomeVersion("0.6.0")
    assert AwesomeVersion("0.6.0-beta.2") > AwesomeVersion("0.6.0-beta.1")
    assert AwesomeVersion(manifest_version()).valid


def test_the_release_workflow_flags_suffixed_tags_as_pre_releases_only():
    assert "--prerelease" in WORKFLOW and "*-*)" in WORKFLOW
    # The flag is set only inside the suffix case, never unconditionally.
    assert WORKFLOW.count("--prerelease") == 1
    assert 'flags=""' in WORKFLOW and "$flags" in WORKFLOW
