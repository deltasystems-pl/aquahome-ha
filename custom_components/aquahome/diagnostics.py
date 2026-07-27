"""Diagnostics support for the AquaHome integration.

Produces a support-ready, deeply redacted snapshot of a config entry: the stored
entry data, every device's parsed :class:`~.api.models.Device` view and its
:class:`~.coordinator.DeviceActivity` feed, each coordinator's liveness signals,
and the client's latest rate-limit telemetry. Users routinely paste diagnostics
into public issue trackers, so anything that identifies the account, the physical
unit, the household, or its location must never survive into the output.

Redaction is a single recursive pass through
:func:`homeassistant.components.diagnostics.async_redact_data`, which replaces the
value of any matching key at any depth. Two properties of that helper shape
:data:`TO_REDACT`:

- It matches on *keys*. The raw device-property map is keyed by property name, so
  naming a sensitive property here redacts that whole property entry — value,
  timestamps, and conversions alike — which is exactly what we want for a value
  that would otherwise leak in plain text.
- It recurses into mappings and lists but not tuples. Every credential- or
  identity-bearing field on the models is reachable through the dict/list
  structure produced by :func:`dataclasses.asdict` (the tuple-valued fields —
  alert lists, unit conversions, feature flags — carry no redactable keys), so a
  flat key set is sufficient.

Property redaction is evidence-driven, chosen against the captured fixtures
(``tests/fixtures/properties.json`` / ``device-detail.json``):

- ``product_serial_number`` and the ``serial_number`` property are per-unit
  hardware serials — redacted (``serial_number`` is already covered as a
  top-level device field).
- ``tz_id`` (e.g. ``Europe/Warsaw``) is a city-level IANA zone that pins the
  installation to a place, so it is redacted. ``tz_dev`` (the POSIX rule string,
  e.g. ``CET-1CEST,M3.5.0,M10.5.0/3``) is deliberately KEPT: it only encodes the
  UTC offset and DST rule shared across an entire continent-sized zone, is not
  location-precise, and is useful when debugging the scheduled-regeneration
  timestamp maths.
- ``wifi_ssid`` is redacted defensively for hosts that surface the network name
  as a raw property (the enriched ``wifi_ssid_name`` field is already covered).

Model and firmware identifiers (model string, part numbers, build codes) are
model-level, not unit-unique, and are retained because they are the first thing a
maintainer needs.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.diagnostics import REDACTED, async_redact_data

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import DeviceSettingsDocument
    from .coordinator import AquaHomeConfigEntry

#: Keys whose values are removed from the diagnostics dump wherever they appear.
#: Credentials/tokens live on the config entry; the identity, household, and
#: location keys live on the device view, its embedded owner summary, and the
#: raw-property map (which is keyed by property name — see the module docstring).
TO_REDACT: Final[frozenset[str]] = frozenset(
    {
        # Config-entry credentials and rotating tokens.
        "email",
        "password",
        "access_token",
        "refresh_token",
        # Device identity, household, and owner fields on the Device / enriched
        # blocks and the embedded owner summary.
        "serial_number",
        "thing_name",
        "nickname",
        "image_url",
        "user",
        "first_name",
        "last_name",
        "wifi_ssid_name",
        # Raw-property names whose VALUES identify the physical unit, the
        # household network, or the installation's location.
        "product_serial_number",
        "wifi_ssid",
        "tz_id",
    }
)

#: Settings whose ``current_value`` identifies the unit or its location. Unlike
#: :data:`TO_REDACT` (which matches keys), the per-setting summary is a uniform
#: ``current_value`` key across every setting, so these two are redacted by
#: setting *name* while every other setting keeps its value for support use — the
#: app nickname and the IANA timezone both pin identity/place.
_REDACTED_SETTING_VALUES: Final[frozenset[str]] = frozenset({"nickname", "timezone"})


def _settings_section(document: DeviceSettingsDocument) -> list[dict[str, Any]]:
    """Return a redaction-safe per-setting summary of a settings document.

    Each entry carries the setting's identity (name, component_type, label), its
    current value, and whether it is visible under the document's conditional
    rules. The nickname and timezone values are pre-redacted here (they are not
    key-addressable by the recursive pass); every other value is kept because a
    support dump needs the actual configuration.
    """
    return [
        {
            "name": setting.name,
            "component_type": setting.component_type,
            "label": setting.label,
            "current_value": REDACTED
            if setting.name in _REDACTED_SETTING_VALUES
            else setting.current_value,
            "visible": document.setting_visible(setting),
        }
        for setting in document.settings
    ]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AquaHomeConfigEntry
) -> dict[str, Any]:
    """Return a redacted diagnostics snapshot for one AquaHome config entry.

    Assembles the entry data, a per-device block (the parsed device view, its
    activity feed and settings summary when loaded, and both liveness signals),
    and the shared client's latest rate-limit telemetry, then redacts the whole
    structure in one recursive pass. ``datetime`` objects are left intact for Home
    Assistant's diagnostics JSON encoder to serialize. Every field is read
    defensively so a coordinator that has not yet produced data dumps ``None``
    rather than raising.
    """
    runtime = entry.runtime_data
    devices: list[dict[str, Any]] = []
    for device_id, coordinator in runtime.coordinators.items():
        activity = runtime.activity_coordinators.get(device_id)
        activity_data = activity.data if activity is not None else None
        settings = runtime.settings_coordinators.get(device_id)
        settings_data = settings.data if settings is not None else None
        scheduler = runtime.schedulers.get(device_id)
        automation = scheduler.data if scheduler is not None else None
        device = coordinator.data
        devices.append(
            {
                "device": dataclasses.asdict(device) if device is not None else None,
                "activity": dataclasses.asdict(activity_data)
                if activity_data is not None
                else None,
                "settings": _settings_section(settings_data)
                if settings_data is not None
                else None,
                "automation": dataclasses.asdict(automation)
                if automation is not None
                else None,
                "device_online": coordinator.device_online,
                "last_update_success": coordinator.last_update_success,
            }
        )

    rate_limit = runtime.client.rate_limit
    payload: dict[str, Any] = {
        "entry": dict(entry.data),
        "devices": devices,
        "rate_limit": dataclasses.asdict(rate_limit)
        if rate_limit is not None
        else None,
    }
    return async_redact_data(payload, TO_REDACT)
