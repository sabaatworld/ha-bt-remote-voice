"""Every user-visible string must come from HA's translation framework."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.selector import SelectSelector

COMPONENT = Path(__file__).parents[1] / "custom_components" / "atvv_remote"
STRINGS = json.loads((COMPONENT / "strings.json").read_text())
ADDRESS = "02:00:00:00:00:01"


def flatten(prefix, node, out):
    for key, value in node.items():
        if isinstance(value, dict):
            flatten(f"{prefix}.{key}", value, out)
        else:
            out[f"{prefix}.{key}"] = value
    return out


def _value(option):
    return option["value"] if isinstance(option, dict) else option


def field_names(result):
    return {str(key) for key in result["data_schema"].schema}


def selectors(result):
    return {
        str(key): value
        for key, value in result["data_schema"].schema.items()
        if isinstance(value, SelectSelector)
    }


async def _forms(hass):
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    from custom_components.atvv_remote.bluez import Remote
    from custom_components.atvv_remote.config_flow import ConfigFlow, OptionsFlow

    hass.states.async_set(
        "media_player.test_speaker",
        "idle",
        {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
    )
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    remote = Remote(ADDRESS, "Test Remote", "/org/bluez/hci1/dev_test", False, True)
    with (
        patch(
            "custom_components.atvv_remote.config_flow.BlueZTransport.list_paired",
            AsyncMock(return_value=[remote]),
        ),
        patch(
            "custom_components.atvv_remote.config_flow.pipelines", return_value={"p": "Pipeline"}
        ),
    ):
        user = await flow.async_step_user()
        manual = await flow.async_step_manual()
        flow.selected = remote
        configure = await flow.async_step_configure()
        entry = SimpleNamespace(data={"pipeline_id": "p"}, options={})
        options_flow = OptionsFlow(entry)
        options_flow.hass = hass
        options = await options_flow.async_step_init()
    return {
        "config": {"user": user, "manual": manual, "configure": configure},
        "options": {"init": options},
    }


async def test_en_json_matches_strings_json():
    assert json.loads((COMPONENT / "translations" / "en.json").read_text()) == STRINGS


async def test_every_form_field_has_a_label_and_a_description(hass):
    forms = await _forms(hass)
    for flow, steps in forms.items():
        for step, result in steps.items():
            node = STRINGS[flow]["step"][step]
            assert node["title"] and node["description"]
            for name in field_names(result):
                assert node["data"].get(name), f"{flow}.{step}.{name} has no label"
                assert node["data_description"].get(name), f"{flow}.{step}.{name} has no help"
                assert "_" not in node["data"][name]


async def test_choice_fields_use_translated_selectors(hass):
    from custom_components.atvv_remote.preferences import CHOICES

    forms = await _forms(hass)
    options = selectors(forms["options"]["init"])
    for key, values in CHOICES.items():
        assert key in options, f"{key} must be a SelectSelector"
        config = options[key].config
        assert config["translation_key"] in STRINGS["selector"]
        translated = STRINGS["selector"][config["translation_key"]]["options"]
        assert {_value(o) for o in config["options"]} == set(values) == set(translated)
    assert selectors(forms["options"]["init"])["mode"].config["translation_key"] == "voice_mode"
    speaker = selectors(forms["options"]["init"])["speaker"].config
    assert "none" in {_value(o) for o in speaker["options"]}
    assert speaker["translation_key"] in STRINGS["selector"]
    device = selectors(forms["config"]["user"])["device"].config
    assert device["translation_key"] in STRINGS["selector"]
    assert "manual" in STRINGS["selector"][device["translation_key"]]["options"]


async def test_flow_errors_and_aborts_exist_in_translations():
    source = (COMPONENT / "config_flow.py").read_text()
    config_source, options_source = source.split("class OptionsFlow")

    def used(text):
        errors = set(re.findall(r'errors\[[^\]]+\]\s*=\s*"(\w+)"', text))
        errors |= set(re.findall(r'"(\w+)" if len', text))  # conditional error codes
        errors |= set(re.findall(r'else "(\w+)"', text))
        return errors, set(re.findall(r'reason="(\w+)"', text))

    for flow, text in (("config", config_source), ("options", options_source)):
        errors, aborts = used(text)
        assert errors <= set(STRINGS[flow]["error"]), errors - set(STRINGS[flow]["error"])
        assert aborts <= set(STRINGS[flow]["abort"]), aborts - set(STRINGS[flow]["abort"])
    assert "already_configured" in STRINGS["config"]["abort"]


def test_no_entity_hardcodes_a_name():
    for path in COMPONENT.glob("*.py"):
        assert "_attr_name" not in path.read_text(), path.name


def _coordinator():
    return SimpleNamespace(
        entry=SimpleNamespace(data={"address": ADDRESS, "name": "Test"}, options={}),
        listeners=set(),
        event_listeners=set(),
        ready=True,
        last_failure=None,
        last_error=None,
        last_outcome=None,
        mic_active=False,
        sessions=0,
        failures=0,
        rejected=0,
        queued=0,
        hass=SimpleNamespace(),
        run_pipeline=None,
    )


def test_every_entity_has_a_translated_name_and_attribute_names():
    from custom_components.atvv_remote.assist_satellite import RemoteSatellite
    from custom_components.atvv_remote.binary_sensor import MicrophoneActive
    from custom_components.atvv_remote.event import VoiceButton
    from custom_components.atvv_remote.sensor import LastError

    c = _coordinator()
    for domain, cls in (
        ("assist_satellite", RemoteSatellite),
        ("event", VoiceButton),
        ("binary_sensor", MicrophoneActive),
        ("sensor", LastError),
    ):
        entity = cls(c)
        key = entity.translation_key
        assert key, cls.__name__
        node = STRINGS["entity"][domain][key]
        assert node["name"]
        attributes = set(node.get("state_attributes", {}))
        if domain in ("assist_satellite", "sensor"):
            assert set(entity.extra_state_attributes) <= attributes, cls.__name__
    event = STRINGS["entity"]["event"]["voice_button"]["state_attributes"]
    assert set(VoiceButton(c)._attr_event_types) == set(event["event_type"]["state"])
    assert {"outcome", "stage", "code", "message"} <= set(event)


def test_error_codes_and_translated_states_agree():
    from custom_components.atvv_remote.preferences import ERROR_CODES

    states = set(STRINGS["entity"]["sensor"]["last_error"]["state"])
    assert states == {"none", "other", *ERROR_CODES}


def test_unknown_error_codes_report_other_with_the_raw_code():
    from custom_components.atvv_remote.sensor import LastError

    c = _coordinator()
    sensor = LastError(c)
    assert sensor.native_value == "none"
    c.last_failure = {
        "code": "stt-provider-exploded",
        "stage": "processing",
        "since": "t",
        "session": 3,
    }
    assert sensor.native_value == "other"
    assert sensor.extra_state_attributes["code"] == "stt-provider-exploded"
    c.last_failure["code"] = "capture_limit"
    assert sensor.native_value == "capture_limit"
    assert sensor.device_class == "enum"
    assert set(sensor.options) == set(STRINGS["entity"]["sensor"]["last_error"]["state"])


def test_error_messages_come_from_translations_and_fall_back_to_other():
    from custom_components.atvv_remote.preferences import error_message

    cache = flatten("component.atvv_remote", {"entity": STRINGS["entity"]}, {})
    hass = SimpleNamespace(config=SimpleNamespace(language="en"))
    with patch(
        "custom_components.atvv_remote.preferences.async_get_cached_translations",
        return_value=cache,
    ):
        assert error_message(hass, "busy") == "Still answering the previous request."
        assert (
            error_message(hass, "something_new")
            == STRINGS["entity"]["sensor"]["last_error"]["state"]["other"]
        )
        assert error_message(hass, None) is None
    assert error_message(SimpleNamespace(), "busy") is None


@pytest.mark.parametrize("language", ["en"])
async def test_translations_are_preloaded_for_runtime_messages(hass, language):
    from custom_components.atvv_remote import preload_translations

    with patch(
        "custom_components.atvv_remote.async_get_translations", AsyncMock(return_value={})
    ) as load:
        await preload_translations(hass)
    load.assert_awaited_once()
    assert load.await_args.args[2] == "entity" and load.await_args.args[3] == ["atvv_remote"]


def test_every_emitted_failure_code_has_a_translated_state():
    """Codes the source can report must be ERROR_CODES entries, not silent 'other'."""
    from custom_components.atvv_remote.preferences import ERROR_CODES

    not_failures = {
        "invalid_preferences",
        "wake_words_not_supported",
        "unloaded",
        "replacement",
        "reply_skipped",
        "condition_false",
        "condition_error",
    }
    emitted = set()
    for path in COMPONENT.glob("*.py"):
        if path.name == "config_flow.py":
            continue  # form errors have their own translations
        text = path.read_text()
        for pattern in (
            r'last_error = "(\w+)"',
            r'AudioFailure\("(\w+)"\)',
            r'abort\("(\w+)"\)',
            r'(?:ValueError|ConnectionError)\("(\w+)"\)',
            r'outcome\("\w+", "(\w+)"\)',
            r'(?:reason|code)="(\w+)"',
        ):
            emitted |= set(re.findall(pattern, text))
    missing = emitted - set(ERROR_CODES) - not_failures
    assert not missing, sorted(missing)


def test_step_description_explains_the_reply_condition_because_its_editor_shows_no_help():
    """HA's condition selector renders only a label, so the dialog intro carries the help."""
    text = STRINGS["options"]["step"]["init"]["description"].lower()
    for needle in (
        "reply condition",
        "empty",
        "start of each request",
        "speaker",
        "remote",
        "held",
    ):
        assert needle in text, needle


def test_every_repair_issue_has_a_title_and_description():
    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "custom_components" / "atvv_remote"
    issues = json.loads((root / "strings.json").read_text())["issues"]
    assert set(issues) == {"pipeline_missing", "speaker_missing"}
    for text in issues.values():
        assert text["title"] and "Configure" in text["description"]
