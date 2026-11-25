import json
import logging
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import condition
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.selector import ConditionSelector

from custom_components.bt_remote_voice.reply_condition import (
    ReplyCondition,
    ReplyConditionSelector,
    load_conditions,
)

RAW = [
    {"condition": "state", "entity_id": "binary_sensor.office_occupancy", "state": "on"},
    {"condition": "template", "value_template": "{{ speaker == 'media_player.x' }}"},
]


async def test_selector_returns_json_safe_raw_list(hass):
    out = ReplyConditionSelector()(RAW)
    assert out == RAW
    assert json_bytes(out)
    # Why the subclass exists: the stock selector returns objects the config entry cannot store.
    with pytest.raises(TypeError):
        json_bytes(ConditionSelector()(RAW))


async def test_malformed_condition_is_rejected_by_selector(hass):
    with pytest.raises(Exception, match="(?i)condition"):
        ReplyConditionSelector()([{"entity_id": "binary_sensor.x"}])  # no "condition" key


async def test_unknown_condition_type_is_rejected_by_the_validator(hass):
    # The selector only checks structure; unknown types fail in the async validator,
    # which is what the options flow uses for the invalid_reply_condition error.
    raw = ReplyConditionSelector()([{"condition": "no_such_condition"}])
    with pytest.raises(Exception, match="(?i)invalid condition"):
        await condition.async_validate_conditions_config(hass, load_conditions(raw))


async def test_selector_serializes_like_the_stock_one(hass):
    assert ReplyConditionSelector().serialize() == ConditionSelector().serialize()


async def test_load_conditions_compiles_stored_raw_list(hass):
    compiled = load_conditions(RAW)
    assert len(compiled) == 2
    validated = await condition.async_validate_conditions_config(hass, compiled)
    assert len(validated) == 2


# ---- B1: the ReplyCondition helper ----
STATE = [{"condition": "state", "entity_id": "binary_sensor.office_occupancy", "state": "on"}]
TEMPLATE = [
    {
        "condition": "template",
        "value_template": "{{ speaker == 'media_player.x' and remote == 'Test' and held is false }}",
    }
]


async def _prepared(hass, raw):
    rc = ReplyCondition(hass, raw)
    await rc.async_prepare()
    return rc


async def test_empty_condition_always_speaks(hass):
    for raw in (None, []):
        assert (await _prepared(hass, raw)).decide({}) == (True, None)


async def test_state_condition_true_and_false(hass):
    rc = await _prepared(hass, STATE)
    hass.states.async_set("binary_sensor.office_occupancy", "on")
    assert rc.decide({}) == (True, None)
    hass.states.async_set("binary_sensor.office_occupancy", "off")
    assert rc.decide({}) == (False, "condition_false")


async def test_template_condition_sees_speaker_remote_held(hass):
    rc = await _prepared(hass, TEMPLATE)
    ok = {"speaker": "media_player.x", "remote": "Test", "held": False}
    assert rc.decide(ok) == (True, None)
    assert rc.decide({**ok, "held": True}) == (False, "condition_false")
    assert rc.decide({**ok, "speaker": "media_player.y"}) == (False, "condition_false")


async def test_missing_entity_is_condition_error_and_logged_once(hass, caplog):
    rc = await _prepared(hass, STATE)  # entity never created
    with caplog.at_level(logging.WARNING):
        results = [rc.decide({}) for _ in range(3)]
    assert results == [(False, "condition_error")] * 3
    assert sum("reply condition" in r.getMessage().lower() for r in caplog.records) == 1


async def test_template_error_is_condition_error_not_exception(hass):
    raw = [{"condition": "template", "value_template": "{{ nosuch.attr == 1 }}"}]
    assert (await _prepared(hass, raw)).decide({}) == (False, "condition_error")


async def test_false_wins_over_error(hass):
    raw = STATE + [{"condition": "template", "value_template": "{{ false }}"}]
    assert (await _prepared(hass, raw)).decide({}) == (False, "condition_false")


async def test_invalid_saved_config_decides_condition_error(hass, caplog):
    with caplog.at_level(logging.WARNING):
        rc = await _prepared(hass, [{"condition": "no_such_condition"}])
        assert rc.decide({}) == (False, "condition_error")
        assert rc.decide({}) == (False, "condition_error")
    assert sum("reply condition" in r.getMessage().lower() for r in caplog.records) == 1


async def test_unload_unloads_checkers(hass):
    rc = await _prepared(hass, STATE + TEMPLATE)
    checkers = list(rc.checkers)
    assert len(checkers) == 2
    rc.async_unload()
    assert all(c._unloaded for c in checkers)


# ---- B3: the options field, validation through the real flow manager, README ----
ROOT = Path(__file__).parents[1]
GOOD = [
    {"condition": "state", "entity_id": "binary_sensor.office_occupancy", "state": "on"},
    {"condition": "template", "value_template": "{{ speaker == 'media_player.x' }}"},
]


def _entry(hass, options=None):
    path = ROOT / "custom_components" / "bt_remote_voice"
    manifest = json.loads((path / "manifest.json").read_text())
    loader.async_setup(hass)
    integration = loader.Integration(
        hass, "custom_components.bt_remote_voice", path, manifest, {x.name for x in path.iterdir()}
    )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {"bt_remote_voice": integration}
    entry = ConfigEntry(
        domain="bt_remote_voice",
        title="Test Remote",
        version=1,
        minor_version=1,
        data={"address": "02:00:00:00:00:01", "name": "Test Remote", "pipeline_id": "p"},
        options=options or {},
        source="user",
        unique_id="02:00:00:00:00:01",
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


async def _open_settings(hass, entry):
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["type"] == "menu" and menu["menu_options"] == ["settings", "test", "report"]
    return await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "settings"}
    )


async def _submit(hass, entry, condition_value):
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}):
        form = await _open_settings(hass, entry)
        return form, await hass.config_entries.options.async_configure(
            form["flow_id"],
            {
                "pipeline_id": "p",
                "mode": "commands",
                "speaker": "none",
                "reply_condition": condition_value,
            },
        )


async def test_reply_condition_field_is_fourth(hass):
    entry = _entry(hass)
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}):
        form = await _open_settings(hass, entry)
    keys = [str(k) for k in form["data_schema"].schema]
    assert keys[:4] == ["pipeline_id", "mode", "speaker", "reply_condition"]


async def test_options_flow_round_trip_stores_json_safe_condition(hass):
    entry = _entry(hass)
    _form, result = await _submit(hass, entry, GOOD)
    assert result["type"] == "create_entry", result
    assert entry.options["reply_condition"] == GOOD
    assert json_bytes(dict(entry.options))  # the config entry can actually be saved
    compiled = await condition.async_validate_conditions_config(
        hass, load_conditions(entry.options["reply_condition"])
    )
    assert len(compiled) == 2


async def test_invalid_condition_is_rejected_with_translated_error(hass):
    entry = _entry(hass)
    _form, result = await _submit(hass, entry, [{"condition": "no_such_condition"}])
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_reply_condition"}
    strings = json.loads(
        (ROOT / "custom_components/bt_remote_voice/translations/en.json").read_text()
    )
    assert strings["options"]["error"]["invalid_reply_condition"]


def test_event_types_include_reply_skipped_and_are_translated():
    strings = json.loads(
        (ROOT / "custom_components/bt_remote_voice/translations/en.json").read_text()
    )
    states = strings["entity"]["event"]["voice_button"]["state_attributes"]["event_type"]["state"]
    assert states["reply_skipped"]
    from custom_components.bt_remote_voice.event import VoiceButton
    from tests.test_translations import _coordinator

    assert "reply_skipped" in VoiceButton(_coordinator())._attr_event_types


def test_readme_documents_the_reply_condition_contract():
    readme = (ROOT / "README.md").read_text().lower()
    for needle in (
        "reply condition",
        "`speaker`",
        "`remote`",
        "`held`",
        "empty",
        "start of each request",
        "counts as",
        "reply_skipped",
    ):
        assert needle in readme, needle


# ---- review fixes: unload ordering and setup wiring ----
async def test_decide_after_unload_is_condition_error(hass):
    rc = await _prepared(hass, STATE)
    rc.async_unload()
    assert rc.decide({}) == (False, "condition_error")  # never "speak" because it was unloaded


async def test_setup_builds_the_condition_from_options_and_unload_clears_it(hass):
    from unittest.mock import AsyncMock

    from custom_components.bt_remote_voice import async_setup_entry, async_unload_entry

    entry = _entry(hass, {"reply_condition": GOOD})
    hass.states.async_set("binary_sensor.office_occupancy", "off")
    with (
        patch("custom_components.bt_remote_voice.coordinator.VoiceCoordinator.start"),
        patch("homeassistant.components.http.server.HomeAssistantHTTP.async_bind", AsyncMock()),
    ):
        async with entry.setup_lock:
            assert await async_setup_entry(hass, entry)
        rc = entry.runtime_data.reply_condition
        assert rc.raw == GOOD and len(rc.checkers) == 2
        assert rc.decide({"speaker": "media_player.x", "remote": "T", "held": False}) == (
            False,
            "condition_false",
        )
        assert await async_unload_entry(hass, entry)
        assert rc.checkers == [] and rc.decide({}) == (False, "condition_error")


async def test_failed_platform_setup_unloads_the_prepared_condition(hass):
    from unittest.mock import AsyncMock

    from custom_components.bt_remote_voice import async_setup_entry

    entry = _entry(hass, {"reply_condition": GOOD})
    holder = {}
    real = ReplyCondition.async_unload

    def spy(self):
        holder["unloaded"] = True
        real(self)

    with (
        patch("custom_components.bt_remote_voice.coordinator.VoiceCoordinator.start"),
        patch.object(ReplyCondition, "async_unload", spy),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=RuntimeError("platform failed")),
        ),
    ):
        async with entry.setup_lock:
            with pytest.raises(RuntimeError):
                await async_setup_entry(hass, entry)
    assert holder.get("unloaded") is True


async def test_setup_raises_repairs_for_missing_dependencies_and_unload_clears_them(hass):
    from unittest.mock import AsyncMock

    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    from homeassistant.helpers import issue_registry as ir

    from custom_components.bt_remote_voice import (
        async_remove_entry,
        async_setup_entry,
        async_unload_entry,
    )

    entry = _entry(hass, {"speaker": "media_player.gone", "mode": "commands"})
    registry = ir.async_get(hass)

    def mine():
        return {i.issue_id for i in registry.issues.values() if i.domain == "bt_remote_voice"}

    with (
        patch("custom_components.bt_remote_voice.coordinator.VoiceCoordinator.start"),
        patch("custom_components.bt_remote_voice.issues.pipelines", return_value={}),
        patch("homeassistant.components.http.server.HomeAssistantHTTP.async_bind", AsyncMock()),
    ):
        async with entry.setup_lock:
            assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()
        assert mine() == set()  # deferred: other integrations may not have loaded yet
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert mine() == {
            f"pipeline_missing_{entry.entry_id}",
            f"speaker_missing_{entry.entry_id}",
        }
        # The speaker coming back clears only its own notice.
        hass.states.async_set("media_player.gone", "idle")
        await hass.async_block_till_done()
        assert mine() == {f"pipeline_missing_{entry.entry_id}"}
        assert await async_unload_entry(hass, entry)
        await entry._async_process_on_unload(hass)
        await hass.async_block_till_done()
        # A reload must not reset a dismissal, so only removal clears the notice.
        assert mine() == {f"pipeline_missing_{entry.entry_id}"}
        await async_remove_entry(hass, entry)
    assert mine() == set()
