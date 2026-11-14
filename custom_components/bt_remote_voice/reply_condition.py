"""Decide at request time whether a reply is spoken, using Home Assistant conditions."""

import copy
import logging

from homeassistant.helpers import condition
from homeassistant.helpers.selector import ConditionSelector

_LOGGER = logging.getLogger(__name__)


class ReplyConditionSelector(ConditionSelector):
    """Condition editor whose submitted value is stored as the raw, JSON-safe list.

    The stock selector returns validated objects (templates, time deltas) that a
    config entry cannot serialize. Validate a copy and keep the raw list; compile it
    again with :func:`load_conditions` when the entry loads.
    """

    def __call__(self, data):
        super().__call__(copy.deepcopy(data))
        return data


def load_conditions(raw):
    """Validate a stored raw condition list into objects HA's condition helpers accept."""
    return ConditionSelector()(copy.deepcopy(raw))


class ReplyCondition:
    """The configured conditions, compiled once per config-entry load."""

    def __init__(self, hass, raw_config):
        self.hass = hass
        self.raw = raw_config or []
        self.checkers = []
        self.invalid = False
        self.unloaded = False
        self._logged = set()

    async def async_prepare(self):
        if not self.raw:
            return
        try:
            validated = await condition.async_validate_conditions_config(
                self.hass, load_conditions(self.raw)
            )
            self.checkers = [await condition.async_from_config(self.hass, c) for c in validated]
        except Exception as err:  # noqa: BLE001 - any invalid saved config means "no"
            self.invalid = True
            self._log_once(f"invalid configuration, replies are skipped until fixed: {err}")

    def decide(self, variables):
        """Return (speak, code): code is None, "condition_false" or "condition_error"."""
        if self.invalid or self.unloaded:
            return False, "condition_error"  # never "speak" because the condition went away
        errored = False
        for checker in self.checkers:
            try:
                if checker.async_check(variables=variables) is False:
                    return False, "condition_false"  # a plain "no" wins over errors
            except Exception as err:  # noqa: BLE001 - errors count as "no", like automations
                errored = True
                self._log_once(f"error while evaluating: {err}")
        return (False, "condition_error") if errored else (True, None)

    def async_unload(self):
        self.unloaded = True
        for checker in self.checkers:
            checker.async_unload()
        self.checkers = []

    def _log_once(self, message):
        if message not in self._logged:
            self._logged.add(message)
            _LOGGER.warning("Reply condition: %s", message)
