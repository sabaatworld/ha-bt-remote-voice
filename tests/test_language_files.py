"""Translation files must cover every Home Assistant language and stay in step with English."""

import importlib.util
import json
from pathlib import Path

import pytest
from homeassistant.generated.languages import LANGUAGES

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "translation_tools", ROOT / "scripts/translation_tools.py"
)
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)

OTHERS = sorted(LANGUAGES - {"en"})


def test_language_files_match_home_assistant_languages():
    assert set(tools.languages()) == set(LANGUAGES)


@pytest.mark.parametrize("lang", OTHERS)
def test_language_file_matches_english_structure(lang):
    assert tools.problems(lang) == []


def test_files_are_utf8_json_objects_with_real_characters():
    for lang in tools.languages():
        text = (tools.TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8")
        assert isinstance(json.loads(text), dict)
        assert "\\u" not in text, f"{lang}.json should store characters, not escapes"


def test_source_hashes_current():
    recorded = json.loads(tools.HASHES.read_text())
    assert recorded == tools.source_hashes(tools.load("en")), (
        "English text changed: retranslate every language, then run "
        "`python scripts/translation_tools.py refresh-hashes`"
    )


def test_helper_detects_damage():
    english = tools.flatten(tools.load("en"))
    key = next(k for k, v in english.items() if "{" in v)
    broken = english[key].replace("{", "(", 1)
    assert tools.placeholders(broken) != tools.placeholders(english[key])
    bold = next(k for k, v in english.items() if "**" in v)
    assert tools.markers(english[bold].replace("**", "", 1)) != tools.markers(english[bold])


async def test_home_assistant_loads_a_non_english_file(hass):
    from homeassistant.helpers.translation import async_get_translations

    german = await async_get_translations(hass, "de", "entity", ["bt_remote_voice"])
    english = await async_get_translations(hass, "en", "entity", ["bt_remote_voice"])
    assert german and set(german) == set(english)
    assert german != english
