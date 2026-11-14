"""Configure an already-paired remote; this flow never creates Bluetooth bonds."""

import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.assist_pipeline import async_get_pipelines
from homeassistant.core import callback
from homeassistant.helpers import condition
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .bluez import BlueZTransport
from .fire import FireTransport
from .playback import speaker_choices
from .preferences import (
    CHOICES,
    CONVERSATION_RANGE,
    DEFAULTS,
    DOMAIN,
    FORM_ORDER,
    MAX_HEAD_START,
    PRESS_RANGE,
    REQUEST_RANGE,
    preferences,
)
from .reply_condition import ReplyConditionSelector, load_conditions

NONE = "none"  # sentinel for "no spoken reply"; stored as an empty speaker


def dropdown(options, translation_key):
    """A dropdown whose option labels are translated from translations/en.json."""
    return SelectSelector(
        SelectSelectorConfig(
            options=options, mode=SelectSelectorMode.DROPDOWN, translation_key=translation_key
        )
    )


def pipeline_dropdown(choices):
    """Dropdown of the live Assist pipelines (names are user data, not translated)."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=k, label=v) for k, v in choices.items()],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def pipeline_choices(items):
    return {item.id: item.name for item in items}


def pipelines(hass):
    try:
        return pipeline_choices(async_get_pipelines(hass))
    except KeyError:
        return {}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.remotes = {}
        self.selected = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input:
            if user_input["device"] == "manual":
                return await self.async_step_manual()
            if user_input["device"] in self.remotes:
                return await self.choose(self.remotes[user_input["device"]])
            errors["base"] = "not_paired"
        try:
            remotes = await BlueZTransport().list_paired()
            self.remotes = {r.address: r for r in remotes}
        except Exception:
            errors["base"] = "bluetooth_unavailable"
        choices = [
            SelectOptionDict(value=r.address, label=f"{r.name} ({r.address})")
            for r in self.remotes.values()
        ]
        choices.append(SelectOptionDict(value="manual", label="manual"))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("device"): dropdown(choices, "remote_device")}),
            errors=errors,
        )

    async def async_step_manual(self, user_input=None):
        errors = {}
        if user_input:
            address = user_input["address"].strip().upper()
            if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
                errors["address"] = "invalid_address"
            else:
                try:
                    remotes = await BlueZTransport().list_paired()
                    found = [r for r in remotes if r.address == address]
                    if len(found) == 1:
                        return await self.choose(found[0])
                    errors["base"] = "multiple_adapters" if len(found) > 1 else "not_paired"
                except Exception:
                    errors["base"] = "bluetooth_unavailable"
        return self.async_show_form(
            step_id="manual", data_schema=vol.Schema({vol.Required("address"): str}), errors=errors
        )

    async def choose(self, remote):
        await self.async_set_unique_id(remote.address)
        self._abort_if_unique_id_configured()
        self.selected = remote
        return await self.async_step_configure()

    async def async_step_configure(self, user_input=None):
        choices = pipelines(self.hass)
        if not choices:
            return self.async_abort(reason="no_pipelines")
        errors = {}
        if user_input:
            if user_input["pipeline_id"] not in choices:
                errors["base"] = "invalid_pipeline"
            else:
                fire = self.selected.family == "fire"
                transport = FireTransport() if fire else BlueZTransport()
                try:
                    await transport.open(
                        self.selected.address, *((lambda d: None,) * 2 if fire else (None, None))
                    )
                except ConnectionError as err:
                    if str(err) == "hidraw_unavailable":
                        errors["base"] = "hidraw_unavailable"
                    else:
                        errors["base"] = "wake_remote"
                except ValueError as err:
                    if str(err) == "opus_decoder_unavailable":
                        errors["base"] = "opus_decoder_unavailable"
                    elif str(err) == "hidraw_permission_denied":
                        errors["base"] = "hidraw_permission_denied"
                    else:
                        errors["base"] = "unsupported_remote"
                except Exception:
                    errors["base"] = "bluetooth_unavailable"
                finally:
                    await transport.close()
                if not errors:
                    return self.async_create_entry(
                        title=self.selected.name,
                        data={
                            "address": self.selected.address,
                            "name": self.selected.name,
                            "pipeline_id": user_input["pipeline_id"],
                            "mode": "transcription",
                            "family": self.selected.family,
                        },
                    )
        return self.async_show_form(
            step_id="configure",
            data_schema=vol.Schema({vol.Required("pipeline_id"): pipeline_dropdown(choices)}),
            errors=errors,
        )


class OptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry):
        self.entry = entry

    async def _condition_is_valid(self, raw):
        if not raw:
            return True
        try:
            await condition.async_validate_conditions_config(self.hass, load_conditions(raw))
        except Exception:  # noqa: BLE001 - any validation failure is a form error
            return False
        return True

    async def async_step_init(self, user_input=None):
        choices = pipelines(self.hass)
        speakers = speaker_choices(self.hass)
        if not choices:
            return self.async_abort(reason="no_pipelines")
        errors = {}
        if user_input:
            try:
                preferences(user_input)
            except ValueError:
                errors["base"] = "invalid_preferences"
            if errors:
                pass
            elif not await self._condition_is_valid(user_input.get("reply_condition")):
                errors["base"] = "invalid_reply_condition"
            elif user_input.get("speaker", NONE) not in (
                NONE,
                *speakers,
                *([self.entry.options["speaker"]] if self.entry.options.get("speaker") else []),
            ):
                errors["base"] = "invalid_speaker"
            elif user_input["pipeline_id"] not in choices or user_input["mode"] not in (
                "transcription",
                "commands",
            ):
                errors["base"] = "invalid_pipeline"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **DEFAULTS,
                        **self.entry.options,
                        **user_input,
                        "speaker": ""
                        if user_input.get("speaker", NONE) == NONE
                        else user_input["speaker"],
                    },
                )
        selected = self.entry.options.get("pipeline_id", self.entry.data["pipeline_id"])
        options = self.entry.options
        saved_speaker = options.get("speaker") or ""
        # Saved choices that no longer exist are flagged, never silently replaced.
        if not user_input:
            if selected not in choices:
                errors["pipeline_id"] = "pipeline_missing"
            if saved_speaker and saved_speaker not in speakers:
                errors["speaker"] = "speaker_missing"

        def seconds(key, low, high, step):
            return (
                vol.Optional(key, default=options.get(key, DEFAULTS[key])),
                NumberSelector(
                    NumberSelectorConfig(
                        min=low,
                        max=high,
                        step=step,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
            )

        fields = {
            "pipeline_id": (
                vol.Required("pipeline_id", default=selected)
                if selected in choices
                else vol.Required("pipeline_id"),
                pipeline_dropdown(choices),
            ),
            "mode": (
                vol.Required(
                    "mode",
                    default=options.get("mode", self.entry.data.get("mode", "transcription")),
                ),
                dropdown(["transcription", "commands"], "voice_mode"),
            ),
            "speaker": (
                vol.Optional("speaker", default=options.get("speaker") or NONE),
                dropdown(
                    [
                        SelectOptionDict(value=NONE, label=NONE),
                        *(SelectOptionDict(value=k, label=v) for k, v in speakers.items()),
                        # Keep an offline or removed speaker selectable so it is not dropped.
                        *(
                            [SelectOptionDict(value=saved_speaker, label=saved_speaker)]
                            if saved_speaker and saved_speaker not in speakers
                            else []
                        ),
                    ],
                    "reply_speaker",
                ),
            ),
            "reply_condition": (
                vol.Optional("reply_condition", default=options.get("reply_condition", [])),
                ReplyConditionSelector(),
            ),
            **{
                key: (
                    vol.Optional(key, default=options.get(key, DEFAULTS[key])),
                    dropdown(list(values), key),
                )
                for key, values in CHOICES.items()
            },
            "stream_head_start": (
                vol.Optional(
                    "stream_head_start",
                    default=options.get("stream_head_start", DEFAULTS["stream_head_start"]),
                ),
                NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=MAX_HEAD_START,
                        step=0.5,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
            ),
            "min_press_seconds": seconds("min_press_seconds", *PRESS_RANGE),
            "max_request_seconds": seconds("max_request_seconds", *REQUEST_RANGE),
            "max_conversation_seconds": seconds("max_conversation_seconds", *CONVERSATION_RANGE),
        }
        # Field order is presentation: FORM_ORDER is the single source (see preferences.py).
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({fields[k][0]: fields[k][1] for k in FORM_ORDER if k in fields}),
            errors=errors,
        )
