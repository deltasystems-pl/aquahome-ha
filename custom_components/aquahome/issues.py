"""Repair issues for AquaHome devices: low salt, leaks, and the automation tier.

The salt section watches each device's fast coordinator for the softener's own
``out_of_salt_estimate_days`` countdown (the PRIMARY salt signal — the Phase-6
chemistry estimate is deliberately not used here) and raises one Repairs issue
per device when it runs low: a warning at
:data:`~.const.SALT_DAYS_WARNING_THRESHOLD` days, escalating to error severity
at :data:`~.const.SALT_DAYS_CRITICAL_THRESHOLD`. Each tier releases only after
the countdown recovers past its threshold plus
:data:`~.const.SALT_DAYS_HYSTERESIS`, so the day-to-day wobble of an estimate
hovering at a boundary never flaps the issue in and out of existence.

The leak section watches each device's analytics engine and files a single
error-severity issue when the leak detector confirms a continuous flow at the
urgent tier (:data:`~.const.LEAK_TIER_URGENT_LITERS_PER_DAY`, burst-pipe
scale) — and only then: softer leak evidence stays on the binary sensor and
the event bus (owner decision 2026-07-27). An engine pass that has nothing to
assess (``active is None``) leaves the issue untouched, so a transient
statistics failure can never silently retract a live warning.

The automation tier (Phase 8) adds three more watchers, all of them fed by the
analytics engine and the per-device scheduler that owns the automation flags:

* **Leak while away** — water flowing through a house nobody is in is rarely
  intentional, so while the household is away (a *detected* vacation or a
  *declared* deferral) a confirmed leak at ANY tier files an error-severity
  issue and fires a dedicated bus event once per onset. The urgent-tier issue
  above stands down for the duration: the two would otherwise say the same
  thing twice, and this one says it louder.
* **Vacation-defer suggestion** — a fixable suggestion to stop regenerating a
  softener that is treating no water, offered only while the user has not
  already automated or started the deferral themselves.
* **Quiet-hour proposal** — a fixable suggestion to move the device's
  ``regeneration_time`` into a night hour the learned activity grid shows the
  household never uses. Writing a device setting is the one automation the
  owner decision keeps behind an explicit confirmation, so this is a proposal
  and never an action; the flows that carry it out live in :mod:`.repairs`.

All three keep the detection tier's silence rule: a verdict of ``None`` — the
detectors having nothing to assess — never files and never retracts anything.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Final

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .analytics.model import TIER_INFO, TIER_URGENT
from .api import scaled_value
from .const import (
    DOMAIN,
    EVENT_AQUAHOME,
    EVENT_TYPE_LEAK_WHILE_AWAY,
    QUIET_REGEN_CANDIDATE_HOURS,
    SALT_DAYS_CRITICAL_THRESHOLD,
    SALT_DAYS_HYSTERESIS,
    SALT_DAYS_WARNING_THRESHOLD,
)
from .entity import device_display_name

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .analytics.engine import AquaHomeAnalyticsEngine
    from .analytics.model import AnalyticsResult, GridSummary, LeakState
    from .api import Device, DeviceSettingsDocument
    from .coordinator import (
        AquaHomeConfigEntry,
        AquaHomeCoordinator,
        AquaHomeSettingsCoordinator,
    )
    from .scheduler import AquaHomeRegenScheduler

#: Issue-id prefixes of the automation tier. Each id ends in the device slug;
#: :mod:`.repairs` dispatches its fix flows on the same prefixes, so they are
#: declared once here rather than spelled out on both sides.
LEAK_WHILE_AWAY_ISSUE_PREFIX: Final = "leak_while_away_"
VACATION_DEFER_ISSUE_PREFIX: Final = "vacation_defer_"
REGEN_TIME_ISSUE_PREFIX: Final = "regen_time_"

#: The device setting the quiet-hour proposal reads and its fix flow writes.
SETTING_REGENERATION_TIME: Final = "regeneration_time"

_SECONDS_PER_HOUR: Final = 3600
_HOURS_PER_DAY: Final = 24
_WEEKDAYS: Final = 7
#: Length of :attr:`~.analytics.model.GridSummary.active_hours` (hour of week).
_GRID_HOURS: Final = _WEEKDAYS * _HOURS_PER_DAY


class _Tier(enum.Enum):
    """Severity tier of the low-salt condition."""

    NONE = enum.auto()
    WARNING = enum.auto()
    CRITICAL = enum.auto()


#: Per-tier issue presentation: (translation_key, severity).
_TIER_PRESENTATION = {
    _Tier.WARNING: ("salt_level_low", ir.IssueSeverity.WARNING),
    _Tier.CRITICAL: ("salt_level_critical", ir.IssueSeverity.ERROR),
}


def _out_of_salt_days(device: Device) -> float | None:
    """Return the device's scaled out-of-salt countdown, or ``None``."""
    prop = device.properties.get("out_of_salt_estimate_days")
    return scaled_value(prop) if prop is not None else None


def _next_tier(current: _Tier, days: float | None) -> _Tier:
    """Return the tier for ``days``, honouring the release hysteresis.

    Entry into a tier happens at its threshold; leaving it (downgrade or clear)
    requires the countdown to recover past threshold + hysteresis, so a value
    oscillating around a boundary keeps its current tier.
    """
    if days is None:
        return _Tier.NONE
    if days <= SALT_DAYS_CRITICAL_THRESHOLD:
        return _Tier.CRITICAL
    if current is _Tier.CRITICAL and days <= (
        SALT_DAYS_CRITICAL_THRESHOLD + SALT_DAYS_HYSTERESIS
    ):
        return _Tier.CRITICAL
    if days <= SALT_DAYS_WARNING_THRESHOLD:
        return _Tier.WARNING
    if current in (_Tier.WARNING, _Tier.CRITICAL) and days <= (
        SALT_DAYS_WARNING_THRESHOLD + SALT_DAYS_HYSTERESIS
    ):
        return _Tier.WARNING
    return _Tier.NONE


@callback
def async_setup_salt_issues(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
) -> None:
    """Watch one device's salt countdown and maintain its repair issue.

    Evaluates the current payload immediately, then on every coordinator
    update (the listener is removed on unload). The issue is re-created —
    :func:`~homeassistant.helpers.issue_registry.async_create_issue` updates in
    place — only when the tier or the rendered day count changes, so a steady
    countdown does not churn the issue registry every poll.
    """
    issue_id = f"low_salt_{coordinator.device_slug}"
    tier = _Tier.NONE
    reported_days: int | None = None

    @callback
    def _evaluate() -> None:
        """Re-derive the tier from the latest payload and sync the issue."""
        nonlocal tier, reported_days
        device: Device | None = coordinator.data
        days = _out_of_salt_days(device) if device is not None else None
        new_tier = _next_tier(tier, days)
        # ``days``/``device`` cannot be None below: _next_tier maps None to
        # NONE — the extra checks narrow the types without an assert. The
        # delete is deliberately unconditional (a no-op when absent): the tier
        # is closure-local and resets on reload, so gating it on the tracked
        # tier would leave a pre-reload issue standing after the salt was
        # refilled while the entry was unloaded.
        if new_tier is _Tier.NONE or days is None or device is None:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            tier = _Tier.NONE
            reported_days = None
            return
        new_days = int(days)
        if new_tier is tier and new_days == reported_days:
            return
        translation_key, severity = _TIER_PRESENTATION[new_tier]
        if tier is _Tier.WARNING and new_tier is _Tier.CRITICAL:
            # An observed escalation must reach the user even if the warning
            # was ignored: async_create_issue updates an existing issue in
            # place and deliberately preserves its dismissed_version, so the
            # issue is deleted first to clear the dismissal. Only the genuine
            # WARNING -> CRITICAL transition does this — the first evaluation
            # after a restart re-enters the current tier from NONE, and
            # deleting there would wipe a legitimate dismissal on every boot.
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=severity,
            translation_key=translation_key,
            translation_placeholders={
                "device": device_display_name(device),
                "days": str(new_days),
            },
        )
        tier = new_tier
        reported_days = new_days

    _evaluate()
    entry.async_on_unload(coordinator.async_add_listener(_evaluate))


def _implied_liters(leak: LeakState) -> int:
    """Return the leak's implied daily volume in whole liters (0 when unknown)."""
    return (
        int(leak.implied_liters_per_day)
        if leak.implied_liters_per_day is not None
        else 0
    )


def _household_away(result: AnalyticsResult, scheduler: AquaHomeRegenScheduler) -> bool:
    """Return whether nobody appears to be home right now.

    Two independent signals, either of which counts: the analytics vacation
    verdict (an absence the detector *observed*) and the automation tier's
    vacation deferral (one the household *declared*, through the switch, the
    service or a blueprint). A vacation verdict of ``None`` is not an absence —
    it only means the detector had nothing to assess.
    """
    return result.vacation.active is True or scheduler.state.vacation_deferral


@callback
def async_setup_leak_issues(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    engine: AquaHomeAnalyticsEngine,
    scheduler: AquaHomeRegenScheduler,
) -> None:
    """Watch one device's leak verdict and maintain its urgent repair issue.

    Filed when the analytics leak detector is active at the urgent tier *and*
    the household is home, deleted when the detector confirms the flow has
    stopped (``active`` is ``False``). ``active is None`` — nothing to assess —
    changes nothing in either direction. The issue is re-created only when the
    rendered daily volume changes, so a steady leak does not churn the issue
    registry on every engine pass; a clear-then-refile naturally resets any
    dismissal, which is exactly right for a leak that stopped and started again.

    While the household is away the issue stands down in favour of the louder,
    any-tier :func:`async_setup_leak_away_issues` one — two Repairs entries for
    one leak would only split the user's attention. "Away" also depends on the
    deferral flag, which moves without an analytics pass, so the scheduler is
    subscribed alongside the engine: an away-to-home transition restores the
    urgent issue on the very next evaluation rather than at the next nightly
    run.
    """
    issue_id = f"leak_urgent_{engine.device_slug}"
    reported_liters: int | None = None

    @callback
    def _evaluate() -> None:
        """Sync the repair issue with the engine's latest leak verdict."""
        nonlocal reported_liters
        # ``data`` is populated once the first pass completes, but stays typed
        # non-optional on the coordinator — annotate so the guard survives.
        result: AnalyticsResult | None = engine.data
        if result is None:
            return
        leak = result.leak
        if leak.active is None:
            return
        away = _household_away(result, scheduler)
        urgent = leak.active and leak.tier == TIER_URGENT and not away
        if not urgent:
            if leak.active is False or away:
                ir.async_delete_issue(hass, DOMAIN, issue_id)
                reported_liters = None
            return
        liters = _implied_liters(leak)
        if liters == reported_liters:
            return
        device: Device | None = coordinator.data
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="leak_urgent",
            translation_placeholders={
                "device": device_display_name(device) if device is not None else "?",
                "liters_per_day": str(liters),
            },
        )
        reported_liters = liters

    _evaluate()
    entry.async_on_unload(engine.async_add_listener(_evaluate))
    entry.async_on_unload(scheduler.async_add_listener(_evaluate))


# ---------------------------------------------------------------------------
# Automation tier (Phase 8): leak-while-away plus the two fixable suggestions
# ---------------------------------------------------------------------------


@callback
def async_setup_leak_away_issues(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    engine: AquaHomeAnalyticsEngine,
    scheduler: AquaHomeRegenScheduler,
) -> None:
    """File the loud leak-while-away issue and event for one device.

    Water moving through an empty house is the one leak signal worth escalating
    at *any* tier: nobody is running a tap, so even a slow continuous flow is
    either a fault or damage in progress. While the household is away a live
    leak therefore files an error-severity issue and fires
    :data:`~.const.EVENT_TYPE_LEAK_WHILE_AWAY` on the bus for the leak-alert
    blueprint.

    The event announces the *onset*, not the issue: it fires once when the
    condition starts and only again after the condition has been false in
    between, so a re-rendered issue (the daily volume or the tier moved) never
    re-notifies a household that has already been told. The issue itself is
    deleted as soon as the leak stops or the household comes home — the
    urgent-tier issue then takes over if the flow is severe enough.
    """
    issue_id = f"{LEAK_WHILE_AWAY_ISSUE_PREFIX}{coordinator.device_slug}"
    reported: tuple[int, str] | None = None
    announced = False

    @callback
    def _evaluate() -> None:
        """Sync the away-leak issue and its onset event with the latest verdict."""
        nonlocal reported, announced
        result: AnalyticsResult | None = engine.data
        if result is None:
            return
        leak = result.leak
        if leak.active is None:
            return
        if not leak.active or not _household_away(result, scheduler):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            reported = None
            announced = False
            return
        liters = _implied_liters(leak)
        # The tier only labels the severity here (any tier files this issue),
        # so an unclassified leak is presented as the mildest one.
        tier = leak.tier or TIER_INFO
        if (liters, tier) != reported:
            device: Device | None = coordinator.data
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="leak_while_away",
                translation_placeholders={
                    "device": device_display_name(device)
                    if device is not None
                    else "?",
                    "liters_per_day": str(liters),
                    "tier": tier,
                },
            )
            reported = (liters, tier)
        if announced:
            return
        announced = True
        hass.bus.async_fire(
            EVENT_AQUAHOME,
            {
                "device_id": coordinator.device_id,
                "device": coordinator.device_slug,
                "type": EVENT_TYPE_LEAK_WHILE_AWAY,
                "tier": leak.tier,
                "rate_liters_per_hour": leak.rate_liters_per_hour,
                "implied_liters_per_day": leak.implied_liters_per_day,
            },
        )

    _evaluate()
    entry.async_on_unload(engine.async_add_listener(_evaluate))
    entry.async_on_unload(scheduler.async_add_listener(_evaluate))


@callback
def async_setup_vacation_defer_issues(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    engine: AquaHomeAnalyticsEngine,
    scheduler: AquaHomeRegenScheduler,
) -> None:
    """Offer to defer regenerations while a detected absence lasts.

    Regenerating a softener that is treating no water spends salt, water and
    resin capacity on nothing, so a detected vacation is worth a suggestion —
    but only a suggestion: the deferral is a device-affecting automation and
    every one of those is opt-in (owner decision 2026-07-27). The issue is
    therefore fixable, and its flow (:mod:`.repairs`) starts an *auto* deferral,
    which releases itself when the household returns.

    Nothing is offered to a household that already decided: the issue is
    cleared the moment the deferral runs or the auto-vacation follower is armed,
    and those flags are checked before the detector's verdict so a user acting
    during a data gap still sees the suggestion disappear.
    """
    issue_id = f"{VACATION_DEFER_ISSUE_PREFIX}{coordinator.device_slug}"
    reported_days: int | None = None

    @callback
    def _evaluate() -> None:
        """Sync the deferral suggestion with the vacation verdict and the flags."""
        nonlocal reported_days
        state = scheduler.state
        if state.auto_vacation or state.vacation_deferral:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            reported_days = None
            return
        result: AnalyticsResult | None = engine.data
        if result is None:
            return
        vacation = result.vacation
        if vacation.active is None:
            return
        if not vacation.active:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            reported_days = None
            return
        if vacation.consecutive_days == reported_days:
            return
        device: Device | None = coordinator.data
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="vacation_defer",
            data={"entry_id": entry.entry_id, "device_id": coordinator.device_id},
            translation_placeholders={
                "device": device_display_name(device) if device is not None else "?",
                "consecutive_days": str(vacation.consecutive_days),
            },
        )
        reported_days = vacation.consecutive_days

    _evaluate()
    entry.async_on_unload(engine.async_add_listener(_evaluate))
    entry.async_on_unload(scheduler.async_add_listener(_evaluate))


def _configured_regen_hour(document: DeviceSettingsDocument | None) -> int | None:
    """Return the hour of day the device is set to regenerate at, or ``None``.

    The ``regeneration_time`` setting is a select whose option values are
    seconds-of-day strings (``"7200"`` = 02:00). A document that has not been
    fetched yet, a device that does not offer the setting, and a value that
    cannot be read as a number all mean the same thing: no configured hour to
    judge, so no proposal.
    """
    if document is None:
        return None
    setting = document.get(SETTING_REGENERATION_TIME)
    if setting is None or setting.current_value is None:
        return None
    try:
        seconds = int(float(str(setting.current_value)))
    except (OverflowError, ValueError):
        return None
    return (seconds // _SECONDS_PER_HOUR) % _HOURS_PER_DAY


def _hour_busy(grid: GridSummary, hour: int) -> bool:
    """Return whether the household normally uses water at ``hour`` on any weekday.

    The grid is indexed by hour of week (``weekday(Mon=0) * 24 + hour``), so one
    hour of day maps to seven buckets; a single busy weekday is enough to make
    the hour a bad time to lose softened water to a regeneration.
    """
    return any(
        grid.active_hours[weekday * _HOURS_PER_DAY + hour]
        for weekday in range(_WEEKDAYS)
    )


def _hour_distance(hour: int, other: int) -> int:
    """Return the circular distance in hours between two hours of the day."""
    gap = abs(hour - other) % _HOURS_PER_DAY
    return min(gap, _HOURS_PER_DAY - gap)


def _proposed_quiet_hour(grid: GridSummary, current_hour: int) -> int | None:
    """Return the night hour to propose instead of ``current_hour``, or ``None``.

    Only the sane night hours (:data:`~.const.QUIET_REGEN_CANDIDATE_HOURS`)
    qualify, and only those the grid finds quiet on *every* weekday — a
    regeneration must not be moved onto Sunday's shower hour to escape
    Wednesday's. Among those the nearest to the currently configured hour wins
    (circular distance, so 23:00 is one hour from 00:00), ties going to the
    earlier hour of the day for a stable, predictable answer. A household busy
    at every candidate hour gets no proposal at all rather than a bad one.
    """
    candidates = [
        hour for hour in QUIET_REGEN_CANDIDATE_HOURS if not _hour_busy(grid, hour)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda hour: (_hour_distance(hour, current_hour), hour))


def _hour_label(hour: int) -> str:
    """Return the ``HH:00`` label the device uses for a whole-hour setting."""
    return f"{hour:02d}:00"


@callback
def async_setup_regen_time_issues(  # noqa: PLR0913 - one watcher, four data sources
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    engine: AquaHomeAnalyticsEngine,
    scheduler: AquaHomeRegenScheduler,
    settings: AquaHomeSettingsCoordinator,
) -> None:
    """Propose moving the regeneration time into a learned quiet hour.

    Softened water is unavailable while the device regenerates, so a
    regeneration scheduled into an hour the household actually uses water is a
    daily annoyance the learned activity grid can spot and fix. Moving it
    *writes a device setting*, which the automation tier never does on its own —
    hence a fixable suggestion the user confirms, gated on the smart-regeneration
    opt-in being on.

    The issue is refiled (delete-then-create, which clears any dismissal) when
    the proposal changes, because a different hour is a different question; an
    unchanged proposal is left alone so a steady grid never churns the registry.
    A missing settings document, an unreadable configured hour or a grid that is
    not yet the full hour-of-week shape are all treated as "nothing to assess"
    and change nothing.
    """
    issue_id = f"{REGEN_TIME_ISSUE_PREFIX}{coordinator.device_slug}"
    reported: tuple[int, int] | None = None

    @callback
    def _clear() -> None:
        """Withdraw the proposal, if one is standing."""
        nonlocal reported
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        reported = None

    @callback
    def _evaluate() -> None:
        """Re-derive the quiet-hour proposal and sync the repair issue."""
        nonlocal reported
        if not scheduler.state.smart_regeneration:
            _clear()
            return
        result: AnalyticsResult | None = engine.data
        document: DeviceSettingsDocument | None = settings.data
        current_hour = _configured_regen_hour(document)
        if result is None or current_hour is None:
            return
        grid = result.grid
        if len(grid.active_hours) != _GRID_HOURS:
            return
        if not _hour_busy(grid, current_hour):
            _clear()
            return
        proposal = _proposed_quiet_hour(grid, current_hour)
        if proposal is None:
            _clear()
            return
        if reported == (current_hour, proposal):
            return
        _clear()
        device: Device | None = coordinator.data
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="regen_time",
            data={
                "entry_id": entry.entry_id,
                "device_id": coordinator.device_id,
                "proposed_seconds": str(proposal * _SECONDS_PER_HOUR),
                "proposed_label": _hour_label(proposal),
                "current_label": _hour_label(current_hour),
            },
            translation_placeholders={
                "device": device_display_name(device) if device is not None else "?",
                "current": _hour_label(current_hour),
                "proposed": _hour_label(proposal),
            },
        )
        reported = (current_hour, proposal)

    _evaluate()
    entry.async_on_unload(engine.async_add_listener(_evaluate))
    entry.async_on_unload(scheduler.async_add_listener(_evaluate))
    entry.async_on_unload(settings.async_add_listener(_evaluate))


@callback
def async_setup_automation_issues(  # noqa: PLR0913 - one wiring call per device
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    engine: AquaHomeAnalyticsEngine,
    scheduler: AquaHomeRegenScheduler,
    settings: AquaHomeSettingsCoordinator,
) -> None:
    """Wire every automation-tier repair watcher for one device.

    A single entry point so setup stays one line per device and no watcher can
    be forgotten; each watcher subscribes its own listeners and releases them
    with the config entry.
    """
    async_setup_leak_away_issues(hass, entry, coordinator, engine, scheduler)
    async_setup_vacation_defer_issues(hass, entry, coordinator, engine, scheduler)
    async_setup_regen_time_issues(hass, entry, coordinator, engine, scheduler, settings)


@callback
def async_remove_automation_issues(
    hass: HomeAssistant, entry: AquaHomeConfigEntry
) -> None:
    """Delete every device's automation-tier issues when the entry is removed.

    Mirrors :func:`async_remove_salt_issues` — the ids are rebuilt from the
    device registry, which is the only place the device slugs still exist once
    the entry is gone — and covers all three ids at once, including the two
    fixable ones whose flows would otherwise be offered for an integration that
    is no longer installed.
    """
    prefixes = (
        LEAK_WHILE_AWAY_ISSUE_PREFIX,
        VACATION_DEFER_ISSUE_PREFIX,
        REGEN_TIME_ISSUE_PREFIX,
    )
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain != DOMAIN:
                continue
            for prefix in prefixes:
                ir.async_delete_issue(hass, DOMAIN, f"{prefix}{identifier}")


@callback
def async_remove_leak_issues(hass: HomeAssistant, entry: AquaHomeConfigEntry) -> None:
    """Delete every device's urgent-leak issue when the entry is removed.

    Mirrors :func:`async_remove_salt_issues`: ids are rebuilt from the device
    registry because ``async_remove_entry`` may run on an entry that was never
    loaded, and deleting an id that was never filed is a documented no-op.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                ir.async_delete_issue(hass, DOMAIN, f"leak_urgent_{identifier}")


@callback
def async_remove_salt_issues(hass: HomeAssistant, entry: AquaHomeConfigEntry) -> None:
    """Delete every device's low-salt issue when the entry is removed.

    A Repairs issue outlives its config entry unless deleted explicitly, so
    without this an uninstalled integration would keep nagging until the next
    restart. Called from ``async_remove_entry``, which runs on an entry that
    may never have been loaded, so — like the statistics cleanup — the issue
    ids are rebuilt from the device registry (each AquaHome device identifier
    is exactly the slug the ids are built from) rather than from runtime data.
    Deleting an id that was never filed (leak-detector sub-devices, healthy
    softeners) is a documented no-op.

    Deliberately NOT wired to plain unload: the Repairs registry preserves a
    user's "Ignore" across reloads and restarts via ``dismissed_version``, and
    a delete would wipe it on every entry reload.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                ir.async_delete_issue(hass, DOMAIN, f"low_salt_{identifier}")
