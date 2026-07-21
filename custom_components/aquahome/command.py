"""Shared command dispatch for AquaHome action platforms.

Buttons, the valve, and the leak-detector scan switch all issue the same kind of
fire-and-forget device command through :meth:`~.api.AquaHomeClient.async_send_command`.
This module centralises the one thing they must all do identically: translate the
client's typed error taxonomy into user-facing
:class:`~homeassistant.exceptions.HomeAssistantError` messages, so a rejected or
failed command reads the same regardless of which platform sent it.

The command endpoint is fire-and-forget: an HTTP 200 means only that the cloud
accepted the request, not that the device has acted on it — the effect surfaces on
a later poll. :func:`async_execute_command` therefore returns ``None`` and never
exposes the raw :class:`~.api.CommandResult`.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError

from .api import ApiError, AquaHomeConnectionError, AuthError, RateLimitError
from .api.client import DEFAULT_COMMAND_ACTION
from .const import DOMAIN

if TYPE_CHECKING:
    from .api import AquaHomeClient

#: Statuses the cloud returns when it understood the request but refused it (bad
#: function/action or an unsatisfiable state); mapped to ``command_rejected`` so
#: the user sees a distinct "the device said no" message from a transport failure.
_COMMAND_REJECTED_STATUSES = frozenset(
    {HTTPStatus.BAD_REQUEST, HTTPStatus.UNPROCESSABLE_ENTITY}
)


async def async_execute_command(
    client: AquaHomeClient,
    device_id: str,
    function: str,
    action: str = DEFAULT_COMMAND_ACTION,
) -> None:
    """Send a device command, mapping API failures to user-facing errors.

    Returns ``None`` on success — the command is fire-and-forget (see the module
    docstring). Every failure is raised as a translated
    :class:`~homeassistant.exceptions.HomeAssistantError`: rate limits, auth
    failures, and connection errors map to their shared keys, a 400/422 rejection
    maps to ``command_rejected`` and any other API error to ``command_failed``,
    both carrying the server message as a placeholder.
    """
    try:
        await client.async_send_command(device_id, function, action)
    except RateLimitError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="rate_limited"
        ) from err
    except AuthError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except AquaHomeConnectionError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err
    except ApiError as err:
        key = (
            "command_rejected"
            if err.status in _COMMAND_REJECTED_STATUSES
            else "command_failed"
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"message": str(err)},
        ) from err
