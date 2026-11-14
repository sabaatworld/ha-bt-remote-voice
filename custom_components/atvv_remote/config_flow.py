"""Configure an already-paired remote; this flow never creates Bluetooth bonds."""

import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.assist_pipeline import async_get_pipelines
from homeassistant.core import callback
from homeassistant.helpers.selector import NumberSelector, NumberSelectorConfig, NumberSelectorMode

from .bluez import BlueZTransport
from .playback import speaker_choices
from .preferences import CHOICES, DEFAULTS, MAX_HEAD_START, preferences

DOMAIN = "atvv_remote"


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
        choices = {
            r.address: f"{r.name} ({r.address}) — {'ATVV' if r.compatible else 'wake to check'}"
            for r in self.remotes.values()
        }
        choices["manual"] = "Enter Bluetooth address"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("device"): vol.In(choices)}),
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
                transport = BlueZTransport()
                try:
                    await transport.open(self.selected.address, None, None)
                except ConnectionError:
                    errors["base"] = "wake_remote"
                except ValueError:
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
                        },
                    )
        return self.async_show_form(
            step_id="configure",
            data_schema=vol.Schema({vol.Required("pipeline_id"): vol.In(choices)}),
            errors=errors,
        )


class OptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry):
        self.entry = entry

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
            elif user_input.get("speaker", "") not in speakers:
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
                        "speaker": user_input.get("speaker", ""),
                    },
                )
        selected = self.entry.options.get("pipeline_id", self.entry.data["pipeline_id"])
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    **{
                        vol.Optional(
                            key, default=self.entry.options.get(key, DEFAULTS[key])
                        ): vol.In(values)
                        for key, values in CHOICES.items()
                    },
                    vol.Optional(
                        "stream_head_start",
                        default=self.entry.options.get(
                            "stream_head_start", DEFAULTS["stream_head_start"]
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=MAX_HEAD_START,
                            step=0.5,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.SLIDER,
                        )
                    ),
                    vol.Required(
                        "pipeline_id",
                        default=selected if selected in choices else next(iter(choices)),
                    ): vol.In(choices),
                    vol.Optional("speaker", default=self.entry.options.get("speaker", "")): vol.In(
                        speakers
                    ),
                    vol.Required(
                        "mode",
                        default=self.entry.options.get(
                            "mode", self.entry.data.get("mode", "transcription")
                        ),
                    ): vol.In(
                        {"transcription": "Transcription only", "commands": "Execute commands"}
                    ),
                }
            ),
            errors=errors,
        )
