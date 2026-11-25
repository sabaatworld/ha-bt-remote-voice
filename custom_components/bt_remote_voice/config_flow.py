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
from .flow_steps import FlowStepsMixin, RemoteChoice, configured_report
from .hid_descriptor import parse_reports
from .playback import speaker_choices
from .preferences import (
    CHOICES,
    CONVERSATION_RANGE,
    DEFAULTS,
    DOMAIN,
    FORM_ORDER,
    GAP_RANGE,
    MAX_HEAD_START,
    PIPELINE_PREFERRED,
    PRESS_RANGE,
    REQUEST_RANGE,
    capture_settings,
    preferences,
)
from .remote_types import DEFAULT_REMOTE_TYPE, REMOTE_TYPES, transport_family
from .reply_condition import ReplyConditionSelector, load_conditions
from .report import ReportInput, build_report, config_summary, hid_summary, versions


def remote_label(remote):
    """Picker text: only data (the remote's name, address and product id), no UI wording."""
    product = getattr(remote, "product_id", None)
    return f"{remote.name} ({remote.address})" + (
        f" [0x{product:04x}]" if product is not None else ""
    )


# What was learned about the remote at setup, kept so diagnostics can show it later. No address.
DETECTED_KEYS = (
    "reasons",
    "vendor_id",
    "product_id",
    "modalias",
    "uuids",
    "manufacturer",
    "appearance",
    "adapter",
)


def detected_signals(remote):
    signals = getattr(remote, "signals", None)
    return {k: v for k, v in (signals() if signals else {}).items() if k in DETECTED_KEYS}


NONE = "none"  # sentinel for "no spoken reply"; stored as an empty speaker


def dropdown(options, translation_key):
    """A dropdown whose option labels are translated from translations/en.json."""
    return SelectSelector(
        SelectSelectorConfig(
            options=options, mode=SelectSelectorMode.DROPDOWN, translation_key=translation_key
        )
    )


def pipeline_dropdown(choices):
    """Preferred (translated) first, then the live Assist pipelines (user data, not translated)."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=PIPELINE_PREFERRED, label=PIPELINE_PREFERRED),
                *(SelectOptionDict(value=k, label=v) for k, v in choices.items()),
            ],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="pipeline",
        )
    )


def pipeline_choices(items):
    return {item.id: item.name for item in items}


def pipelines(hass):
    try:
        return pipeline_choices(async_get_pipelines(hass))
    except KeyError:
        return {}


class ConfigFlow(FlowStepsMixin, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.remotes = {}
        self.selected = None
        self.attempt = None  # outcome of the last connection attempt, for the Remote Report
        self.attempts = []  # (remote type, error code) history, newest last, bounded
        self.form = {}  # last submitted setup values, kept across the report/test steps
        self.observation = None  # result of "Test my remote", shown in the report
        self.observe_task = None

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
            SelectOptionDict(value=r.address, label=remote_label(r)) for r in self.remotes.values()
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
        if user_input and (user_input.get("show_report") or user_input.get("test_remote")):
            self.form = {
                k: v for k, v in user_input.items() if k not in ("show_report", "test_remote")
            }
            if user_input.get("test_remote"):
                return await self.async_step_test()
            return await self.async_step_report()
        if user_input:
            if user_input["pipeline_id"] not in (PIPELINE_PREFERRED, *choices):
                errors["base"] = "invalid_pipeline"
            else:
                remote_type = user_input.get("remote_type", DEFAULT_REMOTE_TYPE)
                fire = (
                    transport_family({"remote_type": remote_type}, {"family": self.selected.family})
                    == "fire"
                )
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
                    # Each literal below is a distinct, translated cause; the ATVV wording of
                    # "unsupported_remote" is only ever shown for ATVV remotes.
                    if str(err) == "opus_decoder_unavailable":
                        errors["base"] = "opus_decoder_unavailable"
                    elif str(err) == "hidraw_permission_denied":
                        errors["base"] = "hidraw_permission_denied"
                    elif str(err) == "voice_service_unavailable":
                        errors["base"] = "voice_service_unavailable"
                    elif str(err) == "not_paired":
                        errors["base"] = "not_paired"
                    elif str(err) == "multiple_adapters":
                        errors["base"] = "multiple_adapters"
                    elif fire:
                        errors["base"] = "fire_voice_unsupported"
                    else:
                        errors["base"] = "unsupported_remote"
                except Exception:
                    errors["base"] = "bluetooth_unavailable"
                finally:
                    await transport.close()
                self.attempt = {
                    "error": errors.get("base"),
                    "remote_type": remote_type,
                    "reports": getattr(transport, "descriptor_reports", None),
                    "descriptor_hex": getattr(transport, "descriptor_hex", None),
                    "nodes": getattr(transport, "hidraw_nodes", None),
                }
                self.attempts = [
                    *self.attempts[-4:],
                    {"remote_type": remote_type, "error": errors.get("base")},
                ]
                if not errors:
                    return self.async_create_entry(
                        title=self.selected.name,
                        data={
                            "address": self.selected.address,
                            "name": self.selected.name,
                            "pipeline_id": user_input["pipeline_id"],
                            "mode": "transcription",
                            "family": self.selected.family,
                            "detected": detected_signals(self.selected),
                        },
                        options={"remote_type": remote_type},
                    )
        remembered_pipeline = self.form.get("pipeline_id", PIPELINE_PREFERRED)
        if remembered_pipeline not in (PIPELINE_PREFERRED, *choices):
            remembered_pipeline = PIPELINE_PREFERRED
        remembered_type = self.form.get(
            "remote_type", (self.attempt or {}).get("remote_type", DEFAULT_REMOTE_TYPE)
        )
        return self.async_show_form(
            step_id="configure",
            data_schema=vol.Schema(
                {
                    vol.Required("pipeline_id", default=remembered_pipeline): pipeline_dropdown(
                        choices
                    ),
                    vol.Optional("remote_type", default=remembered_type): dropdown(
                        list(REMOTE_TYPES), "remote_type"
                    ),
                    vol.Optional("test_remote", default=False): bool,
                    vol.Optional("show_report", default=False): bool,
                }
            ),
            errors=errors,
        )

    def remote_report(self):
        """The shareable report for the remote being set up (works before any attempt)."""
        attempt = self.attempt or {}
        chosen = self.form.get("remote_type")
        if attempt and chosen and chosen != attempt.get("remote_type"):
            attempt = {}  # that attempt used another remote type: its outcome is stale
        stage = "not_tried"
        if attempt:
            stage = "setup"
        signals = getattr(self.selected, "signals", None)
        remote_type = chosen or attempt.get("remote_type") or "unknown"
        remote = {**(signals() if signals else {}), "remote_type": remote_type}
        hid = {"nodes": attempt.get("nodes", "unknown")}
        if attempt.get("reports") is not None:
            hid = hid_summary(
                attempt.get("reports"), attempt.get("descriptor_hex"), attempt.get("nodes")
            )
        elif self.observation and self.observation.descriptors:
            hex_ = next(iter(self.observation.descriptors.values()))
            hid = hid_summary(parse_reports(bytes.fromhex(hex_)), hex_, self.observation.nodes)
        return build_report(
            ReportInput(
                **versions(),
                adapter={"name": remote.get("adapter")},
                remote=remote,
                hid=hid,
                config=config_summary(
                    {"remote_type": remote_type, "pipeline_id": self.form.get("pipeline_id")}, {}
                ),
                observation=self.observation.to_dict() if self.observation else {},
                outcome={
                    "error": attempt.get("error") or ("none" if attempt else None),
                    "stage": stage,
                    "attempts": self.attempts,
                },
            )
        )

    # ---- hooks for FlowStepsMixin (the test and report screens) ----
    def test_target(self):
        remote_type = self.form.get("remote_type", DEFAULT_REMOTE_TYPE)
        family = transport_family({"remote_type": remote_type}, {"family": self.selected.family})
        return RemoteChoice(self.selected.address, family, remote_type, self.selected.name)

    def recording_settings(self):
        # A remote being set up has no saved settings: the test records like the defaults.
        return {
            "min_press_seconds": DEFAULTS["min_press_seconds"],
            **capture_settings({}, self.test_target().family),
        }

    def test_entry(self):
        return None  # a remote being set up has no running entry to pause

    async def back_step(self):
        return await self.async_step_configure()


class OptionsFlow(FlowStepsMixin, config_entries.OptionsFlow):
    def __init__(self, entry):
        self.entry = entry

    # ---- hooks for FlowStepsMixin (test and report for a remote that is already set up) ----
    def test_target(self):
        data, options = self.entry.data, self.entry.options
        return RemoteChoice(
            data["address"],
            transport_family(options, data),
            options.get("remote_type", DEFAULT_REMOTE_TYPE),
            data.get("name", ""),
        )

    def recording_settings(self):
        """The saved settings, so the test records the way a real request would."""
        value = self.entry.options.get("min_press_seconds", DEFAULTS["min_press_seconds"])
        usable = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= value <= PRESS_RANGE[1]
        )
        return {
            "min_press_seconds": value if usable else DEFAULTS["min_press_seconds"],
            **capture_settings(self.entry.options, self.test_target().family),
        }

    def test_entry(self):
        return self.entry

    async def back_step(self):
        return await self.async_step_init()

    def remote_report(self):
        return configured_report(self.entry, self.observation)

    async def async_step_init(self, user_input=None):
        """What to do with this remote. The remote and its type are fixed once it is set up."""
        return self.async_show_menu(step_id="init", menu_options=["settings", "test", "report"])

    async def _condition_is_valid(self, raw):
        if not raw:
            return True
        try:
            await condition.async_validate_conditions_config(self.hass, load_conditions(raw))
        except Exception:  # noqa: BLE001 - any validation failure is a form error
            return False
        return True

    async def async_step_settings(self, user_input=None):
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
            elif user_input["pipeline_id"] not in (PIPELINE_PREFERRED, *choices) or user_input[
                "mode"
            ] not in (
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
            if selected != PIPELINE_PREFERRED and selected not in choices:
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
                if selected == PIPELINE_PREFERRED or selected in choices
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
            "ignore_early_stop": (
                vol.Optional(
                    "ignore_early_stop",
                    default=options.get("ignore_early_stop", DEFAULTS["ignore_early_stop"]),
                ),
                bool,
            ),
            "gap_tolerance_ms": (
                vol.Optional(
                    "gap_tolerance_ms",
                    default=options.get("gap_tolerance_ms", DEFAULTS["gap_tolerance_ms"]),
                ),
                NumberSelector(
                    NumberSelectorConfig(
                        min=GAP_RANGE[0],
                        max=GAP_RANGE[1],
                        step=GAP_RANGE[2],
                        unit_of_measurement="ms",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
            ),
            "max_request_seconds": seconds("max_request_seconds", *REQUEST_RANGE),
            "max_conversation_seconds": seconds("max_conversation_seconds", *CONVERSATION_RANGE),
        }
        # Field order is presentation: FORM_ORDER is the single source (see preferences.py).
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema({fields[k][0]: fields[k][1] for k in FORM_ORDER if k in fields}),
            errors=errors,
        )
