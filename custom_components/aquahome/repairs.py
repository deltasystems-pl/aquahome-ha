"""Fix flows for the automation tier's two fixable repair issues.

The automation tier may act on the device by itself — it schedules and cancels
regenerations once the owner has opted in — but it never *writes a device
setting* or starts a vacation deferral without being asked (owner decision
2026-07-27). Those two suggestions are therefore filed by :mod:`.issues` as
fixable Repairs issues, and this module carries out what the user confirms:

* ``vacation_defer_<slug>`` starts the deferral, recorded as an *auto* one so
  it releases itself when the vacation detector sees the household return.
* ``regen_time_<slug>`` writes the proposed ``regeneration_time`` value the
  issue was filed with, so what the user confirmed is exactly what is sent —
  the proposal is never re-derived here from data that has since moved.

Both flows are a single confirmation step rendering the issue's own translation
placeholders (the core :class:`~homeassistant.components.repairs.ConfirmRepairFlow`
shape), and both resolve the objects they act on through the config entry's
runtime data at confirm time rather than capturing them when the issue was
filed: a Repairs card can sit in the sidebar across reloads, and only the
coordinators the current load built are the ones allowed to act. An entry that
is missing or not loaded aborts the flow honestly instead of failing silently.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir

from .api import AquaHomeError
from .const import DEFERRAL_SOURCE_AUTO, DOMAIN
from .issues import (
    REGEN_TIME_ISSUE_PREFIX,
    SETTING_REGENERATION_TIME,
    VACATION_DEFER_ISSUE_PREFIX,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResult

    from .coordinator import AquaHomeSettingsCoordinator
    from .scheduler import AquaHomeRegenScheduler

_LOGGER = logging.getLogger(__name__)

#: Abort reasons (translated under ``issues.<key>.fix_flow.abort`` in strings).
_ABORT_ENTRY_NOT_LOADED = "entry_not_loaded"
_ABORT_WRITE_FAILED = "write_failed"


class _AquaHomeFixFlow(RepairsFlow):
    """Shared confirm-then-act plumbing for the integration's fix flows.

    The flow manager assigns ``hass``, ``handler``, ``issue_id`` and ``data`` to
    the handler it receives from :func:`async_create_fix_flow`; they are also
    taken in the constructor so a flow is fully usable the moment it is built,
    with exactly the issue data it was created for. Subclasses implement
    :meth:`_async_apply`, which runs only after the user confirms.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        issue_id: str,
        data: dict[str, str | int | float | None] | None,
    ) -> None:
        """Bind the flow to the issue it was created for."""
        self.hass = hass
        # Repairs flows are keyed by the integration domain; pre-setting it (to
        # the same value the flow manager assigns) keeps the issue lookup in
        # the confirm step working for a flow that has not been registered.
        self.handler = DOMAIN
        self.issue_id = issue_id
        self.data = data

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Handle the first step of the fix flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Show the confirmation form, then perform the fix once submitted.

        The form carries the issue's own translation placeholders, so the
        confirmation names the device and the concrete values the user is
        agreeing to — the same wiring the core confirm flow uses.
        """
        if user_input is not None:
            return await self._async_apply()
        issue = ir.async_get(self.hass).async_get_issue(self.handler, self.issue_id)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=(
                issue.translation_placeholders if issue is not None else None
            ),
        )

    async def _async_apply(self) -> FlowResult:
        """Carry out the confirmed fix (implemented per issue type)."""
        raise NotImplementedError

    def _issue_value(self, key: str) -> str | None:
        """Return one string field of the issue data, or ``None``.

        Issue data survives a restart as plain JSON, so every read is treated as
        untrusted: a field written by another version simply resolves to
        ``None`` and the flow aborts rather than acting on a guess.
        """
        value = (self.data or {}).get(key)
        return value if isinstance(value, str) else None

    def _loaded_entry(self) -> ConfigEntry | None:
        """Return the loaded config entry this issue belongs to, or ``None``.

        Only a LOADED entry carries ``runtime_data``; an entry that was removed,
        disabled or failed to set up has nothing to act with.
        """
        entry_id = self._issue_value("entry_id")
        if entry_id is None:
            return None
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return None
        return entry

    def _scheduler(self) -> AquaHomeRegenScheduler | None:
        """Return the automation scheduler of this issue's device, or ``None``."""
        entry = self._loaded_entry()
        device_id = self._issue_value("device_id")
        if entry is None or device_id is None:
            return None
        # ``runtime_data`` is untyped on a registry lookup; the annotation is
        # what pins the object this flow is allowed to drive.
        scheduler: AquaHomeRegenScheduler | None = entry.runtime_data.schedulers.get(
            device_id
        )
        return scheduler

    def _settings(self) -> AquaHomeSettingsCoordinator | None:
        """Return the settings coordinator of this issue's device, or ``None``."""
        entry = self._loaded_entry()
        device_id = self._issue_value("device_id")
        if entry is None or device_id is None:
            return None
        coordinator: AquaHomeSettingsCoordinator | None = (
            entry.runtime_data.settings_coordinators.get(device_id)
        )
        return coordinator


class VacationDeferFixFlow(_AquaHomeFixFlow):
    """Confirm deferring regenerations for the detected absence."""

    async def _async_apply(self) -> FlowResult:
        """Start the vacation deferral on the issue's device."""
        scheduler = self._scheduler()
        if scheduler is None:
            return self.async_abort(reason=_ABORT_ENTRY_NOT_LOADED)
        # Recorded as AUTO, not MANUAL: the user confirmed a suggestion made
        # from a *detected* absence, so the deferral is released again by the
        # same detector when the household comes home.
        await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_AUTO)
        return self.async_create_entry(data={})


class RegenTimeFixFlow(_AquaHomeFixFlow):
    """Confirm moving the regeneration time into a learned quiet hour."""

    async def _async_apply(self) -> FlowResult:
        """Write the proposed regeneration time to the device."""
        settings = self._settings()
        if settings is None:
            return self.async_abort(reason=_ABORT_ENTRY_NOT_LOADED)
        proposed = self._issue_value("proposed_seconds")
        if proposed is None:
            return self.async_abort(reason=_ABORT_WRITE_FAILED)
        try:
            await settings.async_write_setting(SETTING_REGENERATION_TIME, proposed)
        except AquaHomeError as err:
            # The user is standing in front of the dialog: the abort tells them
            # plainly that the setting is unchanged, and the issue stays filed
            # so the proposal can be confirmed again later. The cause is logged
            # because the abort string cannot carry it.
            _LOGGER.warning(
                "AquaHome could not write the proposed regeneration time: %s", err
            )
            return self.async_abort(reason=_ABORT_WRITE_FAILED)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Build the fix flow for one of the integration's fixable issues.

    Dispatch is by issue-id prefix — every id ends in the device slug, so one
    flow class serves every device. An id this version does not know (one filed
    by an older release, say) falls back to the core confirm flow, which
    acknowledges and clears it without side effects instead of raising.
    """
    if issue_id.startswith(VACATION_DEFER_ISSUE_PREFIX):
        return VacationDeferFixFlow(hass, issue_id, data)
    if issue_id.startswith(REGEN_TIME_ISSUE_PREFIX):
        return RegenTimeFixFlow(hass, issue_id, data)
    return ConfirmRepairFlow()
