"""The "Test my remote" and "Show remote report" screens, shared by setup and Configure.

``FlowStepsMixin`` gives both the config flow (a remote being set up) and the options flow (a
remote that is already configured) the same steps. A test pauses the live entry first, because
the remote's connection can only be held once, and always restores it. Nothing here imports
``config_flow`` (``issues.py`` imports that module).
"""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .clip_recorder import record_test
from .clips import SavedClips, clip_links, save_clips
from .observe import OBSERVE_SECONDS, Observation, merge_recording, observe
from .preferences import DOMAIN
from .remote_types import transport_family
from .report import ReportInput, build_report, config_summary, versions

_LOGGER = logging.getLogger(__name__)

PRESSED = ("atvv_hold", "atvv_tap", "fire_voice")  # results that mean the voice key was pressed


class PauseRefused(Exception):
    """The live remote could not be paused; the message is a translated abort reason."""


@dataclass(frozen=True)
class RemoteChoice:
    address: str
    family: str
    remote_type: str
    name: str = ""


@asynccontextmanager
async def paused_entry(hass, address, entry=None):
    """Hold the remote for a test: unload its live entry, and always set it up again.

    ``entry`` is None while a remote is being set up (nothing is running for it). Only one test
    per remote at a time. Unload and restore run in tasks that a cancelled flow cannot interrupt.
    """
    running = hass.data.setdefault(DOMAIN, {}).setdefault("tests", set())
    if address in running:
        raise PauseRefused("test_running")
    running.add(address)
    unload = None
    try:
        if entry is not None:
            if entry.state in (
                ConfigEntryState.SETUP_IN_PROGRESS,
                ConfigEntryState.UNLOAD_IN_PROGRESS,
            ):
                raise PauseRefused("test_running")
            if entry.state is not ConfigEntryState.NOT_LOADED:  # loaded, or retrying/failed
                unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
                if not await asyncio.shield(unload):
                    unload = None
                    raise PauseRefused("pause_failed")
        yield
    finally:
        try:
            if unload is not None:
                await asyncio.shield(hass.async_create_task(_restore(hass, entry, unload)))
        finally:
            running.discard(address)


async def _restore(hass, entry, unload):
    """Set the entry up again once its unload has finished (only if it is really unloaded)."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await unload
    if entry.state is not ConfigEntryState.NOT_LOADED:
        _LOGGER.warning("Not restoring %s after the test: state is %s", entry.title, entry.state)
        return
    try:
        await hass.config_entries.async_setup(entry.entry_id)
    except Exception:  # noqa: BLE001 - a restore failure must be logged, never lost silently
        _LOGGER.exception("Could not set %s up again after the test", entry.title)


def configured_report(entry, observation):
    """The shareable report for an already-configured remote (works while its entry is paused)."""
    data, options = entry.data or {}, entry.options or {}
    detected = data.get("detected") or {}
    return build_report(
        ReportInput(
            **versions(),
            adapter={"name": detected.get("adapter")},
            remote={
                **detected,
                "name": data.get("name"),
                "address": data.get("address"),
                "family": transport_family(options, data),
                "remote_type": options.get("remote_type", "auto"),
            },
            hid={"nodes": observation.nodes if observation else "unknown"},
            config=config_summary(options, data),
            observation=observation.to_dict() if observation else {},
            outcome={"error": None, "stage": "not_tried"},
        )
    )


class FlowStepsMixin:
    """Steps ``test``, ``test_result_*``, ``test_clips``, ``test_clips_none`` and ``report``.

    The flow supplies ``test_target``, ``recording_settings``, ``test_entry``, ``back_step`` and
    ``remote_report``; it also owns ``observation``, ``observe_task`` and ``saved``.
    """

    observation = None
    observe_task = None
    saved = None

    async def _observe(self):
        target = self.test_target()
        events, errors, summary = [], [], {}
        async with paused_entry(self.hass, target.address, self.test_entry()):
            ticker = self.hass.async_create_task(self._tick())
            try:
                # Keys come from the kernel input node; the recorder is the only ATVV connection.
                results = await asyncio.gather(
                    observe(target.address, target.family, OBSERVE_SECONDS, atvv=False),
                    record_test(
                        target.address,
                        target.family,
                        OBSERVE_SECONDS,
                        events=events,
                        errors=errors,
                        summary=summary,
                        **self.recording_settings(),
                    ),
                    return_exceptions=True,  # both finish before the live remote is restored
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                observation, clips = results
            finally:
                ticker.cancel()
        merge_recording(observation, events, errors, clips, summary)
        self.saved = await save_clips(self.hass, clips)
        observation.clip_saved = bool(self.saved.names)
        self.observation = observation
        return observation

    async def _tick(self):
        """Move the progress bar once a second, so the screen shows the test is running."""
        started = asyncio.get_running_loop().time()
        while True:
            elapsed = asyncio.get_running_loop().time() - started
            with contextlib.suppress(Exception):  # a missing flow context must not stop the test
                self.async_update_progress(min(1.0, elapsed / OBSERVE_SECONDS))
            await asyncio.sleep(1)

    async def async_step_test(self, user_input=None):
        """Listen to the remote while the user presses its voice button, recording each press."""
        if self.observe_task is None:
            self.saved = SavedClips()
            self.observe_task = self.hass.async_create_task(self._observe())
        if not self.observe_task.done():
            return self.async_show_progress(
                step_id="test", progress_action="observing", progress_task=self.observe_task
            )
        task, self.observe_task = self.observe_task, None
        try:
            observation = task.result()
        except PauseRefused as err:
            return self.async_abort(reason=str(err))
        except asyncio.CancelledError, Exception:  # noqa: BLE001 - a failed listen is a result
            observation = Observation(errors=["bluetooth_unavailable"])
        self.observation = observation
        return self.async_show_progress_done(next_step_id=f"test_result_{observation.result}")

    async def _result_form(self, kind, user_input):
        if user_input is not None:
            if self.saved and self.saved.names:
                return await self.async_step_test_clips()
            if kind in PRESSED:
                return await self.async_step_test_clips_none()
            return await self._after_clips()
        return self.async_show_form(step_id=f"test_result_{kind}", data_schema=vol.Schema({}))

    async def async_step_test_result_atvv_hold(self, user_input=None):
        return await self._result_form("atvv_hold", user_input)

    async def async_step_test_result_atvv_tap(self, user_input=None):
        return await self._result_form("atvv_tap", user_input)

    async def async_step_test_result_fire_voice(self, user_input=None):
        return await self._result_form("fire_voice", user_input)

    async def async_step_test_result_plain_key(self, user_input=None):
        return await self._result_form("plain_key", user_input)

    async def async_step_test_result_nothing(self, user_input=None):
        return await self._result_form("nothing", user_input)

    async def _after_clips(self):
        """Tell the user what the test noticed (early stops, lost audio), then go back."""
        observation = self.observation
        if observation and (observation.early_stops or observation.audio_gaps):
            return await self.async_step_test_hints()
        return await self.back_step()

    async def async_step_test_hints(self, user_input=None):
        if user_input is not None:
            return await self.back_step()
        return self.async_show_form(
            step_id="test_hints",
            data_schema=vol.Schema({}),
            description_placeholders={
                "early": str(self.observation.early_stops),
                "gaps": str(self.observation.audio_gaps),
            },
        )

    async def async_step_test_clips(self, user_input=None):
        if user_input is not None:
            return await self._after_clips()
        return self.async_show_form(
            step_id="test_clips",
            data_schema=vol.Schema({}),
            # Signed here, not earlier: a link is good for five minutes from when it is shown.
            description_placeholders={"clips": clip_links(self.hass, self.saved)},
        )

    async def async_step_test_clips_none(self, user_input=None):
        if user_input is not None:
            return await self._after_clips()
        return self.async_show_form(step_id="test_clips_none", data_schema=vol.Schema({}))

    @callback
    def async_remove(self):
        if self.observe_task and not self.observe_task.done():
            self.observe_task.cancel()  # paused_entry restores the live remote on the way out

    async def async_step_report(self, user_input=None):
        """Show the report to copy into an issue; edits are ignored; Submit goes back."""
        if user_input is not None:
            return await self.back_step()
        schema = vol.Schema(
            {vol.Optional("report"): TextSelector(TextSelectorConfig(multiline=True))}
        )
        return self.async_show_form(
            step_id="report",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"report": self.remote_report()}
            ),
        )
