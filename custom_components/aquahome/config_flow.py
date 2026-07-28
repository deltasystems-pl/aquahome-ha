"""Config and reauth flow for the AquaHome integration.

The iQua cloud is split across two byte-identical hosts: legacy accounts live on
``api.myiquaapp.com`` and post-migration ("iQua2") accounts on ``api.iqua2.com``.
A login simply fails on the wrong one, so the flow cannot ask the user which host
they belong to — it probes both. :func:`_async_probe_account` encodes the exact
tie-break this demands: a wrong-host login is only reported as
``invalid_auth`` when *both* hosts reject the credentials, only ``cannot_connect``
when *both* are unreachable, and a host that authenticates but returns no devices
never masks a host that has them. The working host and its token pair are then
persisted on the config entry so every later request targets the right database.

Two account states interrupt the happy path and are handled as dedicated steps:
an unverified account (:class:`UserNotVerifiedError`) is routed to the ``verify``
step to clear the emailed confirmation-code challenge, and an expired/rotated
refresh token drives Home Assistant's reauth flow. Reauth re-runs the *same*
probe, which is what transparently heals an account that was migrated to the
other host since it was first configured. Passwords and tokens are never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_CODE,
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    API_BASE_URL,
    IQUA2_BASE_URL,
    ApiError,
    AquaHomeClient,
    AquaHomeConnectionError,
    AquaHomeError,
    AuthError,
    AuthManager,
    Device,
    LoginResult,
    RateLimitError,
    UserNotVerifiedError,
)
from .const import (
    CONF_REFRESH_TOKEN,
    CONFIG_MINOR_VERSION,
    CONFIG_VERSION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

#: Hosts probed, in order, by :func:`_async_probe_account`. The legacy host is
#: first so an unverified-account challenge (which propagates immediately) always
#: originates there, matching where a fresh confirmation code is requested.
_PROBE_HOSTS: Final = (API_BASE_URL, IQUA2_BASE_URL)

_USER_SCHEMA: Final = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)

_REAUTH_SCHEMA: Final = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)

_VERIFY_SCHEMA: Final = vol.Schema(
    {vol.Required(CONF_CODE): TextSelector(TextSelectorConfig())}
)


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """A successful probe of one iQua host.

    ``devices`` may be empty: a host that authenticates but returns no devices is
    still a valid outcome (the caller shows ``no_devices`` on a fresh install but
    accepts it during reauth).
    """

    host: str
    login: LoginResult
    devices: list[Device]


async def _async_probe_account(
    session: aiohttp.ClientSession, email: str, password: str, language: str
) -> ProbeOutcome:
    """Find the iQua host that owns ``email``/``password`` and list its devices.

    Each host is tried in order with a throwaway :class:`AuthManager`. The result
    resolves ambiguity conservatively:

    - a non-empty device list on any host wins immediately;
    - an unverified-account challenge or a rate-limit response propagates at once
      (both are account/server state, not a wrong host, so hammering the other
      host would be wrong);
    - a host that authenticates but returns no devices is remembered and probing
      continues, so a second host with devices can still win;
    - only when no host yields devices does the recorded failure surface, and a
      connection failure is preferred over an auth failure (the unreachable host
      may be the account's real home, so ``cannot_connect`` beats ``invalid_auth``
      in the mixed case).

    Raises :class:`UserNotVerifiedError`, :class:`RateLimitError`,
    :class:`AuthError`, :class:`ApiError`, or :class:`AquaHomeConnectionError`.
    """
    auth_error: AuthError | None = None
    connection_error: AquaHomeConnectionError | ApiError | None = None
    empty_outcome: ProbeOutcome | None = None
    for host in _PROBE_HOSTS:
        manager = AuthManager(session, base_url=host)
        try:
            login = await manager.async_login(email, password)
        except UserNotVerifiedError:
            raise
        except RateLimitError:
            raise
        except AuthError as err:
            auth_error = err
            continue
        except (AquaHomeConnectionError, ApiError) as err:
            connection_error = err
            continue
        client = AquaHomeClient(session, manager, base_url=host, language=language)
        try:
            devices = await client.async_get_devices()
        except RateLimitError:
            raise
        except (AquaHomeConnectionError, ApiError) as err:
            connection_error = err
            continue
        if devices:
            return ProbeOutcome(host=host, login=login, devices=devices)
        if empty_outcome is None:
            empty_outcome = ProbeOutcome(host=host, login=login, devices=devices)
    if empty_outcome is not None:
        return empty_outcome
    if connection_error is not None:
        raise connection_error
    if auth_error is not None:
        raise auth_error
    # Unreachable while _PROBE_HOSTS is a non-empty module constant, so it is
    # excluded from coverage rather than deleted.
    msg = "No API hosts were provided to probe"  # pragma: no cover
    raise AquaHomeConnectionError(msg)  # pragma: no cover


class AquaHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the AquaHome config, verification, and reauth flow."""

    VERSION = CONFIG_VERSION
    MINOR_VERSION = CONFIG_MINOR_VERSION

    #: Account email, from the user form or the reauth entry; also the verify
    #: challenge target. Always set before any step that reads it runs.
    _email: str
    #: Password to probe with, from the user or reauth form. Never logged.
    _password: str

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and probe both iQua hosts for the account."""
        if user_input is None:
            return self._async_show_form("user", {})
        self._email = user_input[CONF_EMAIL]
        self._password = user_input[CONF_PASSWORD]
        return await self._async_probe_and_finish("user")

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Begin reauthentication of an existing entry (password re-entry)."""
        self._email = self._get_reauth_entry().data[CONF_EMAIL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-probe with a freshly entered password to heal the stored tokens."""
        if user_input is None:
            return self._async_show_form("reauth_confirm", {})
        self._password = user_input[CONF_PASSWORD]
        return await self._async_probe_and_finish("reauth_confirm")

    async def async_step_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Clear an unverified-account challenge with the emailed code.

        On success the full probe is re-run and the flow finishes exactly like the
        user (or reauth) success path — the account is now verified.
        """
        if user_input is None:
            return self._async_show_form("verify", {})
        errors: dict[str, str] = {}
        manager = AuthManager(async_get_clientsession(self.hass), base_url=API_BASE_URL)
        try:
            await manager.async_validate_user(self._email, user_input[CONF_CODE])
        except RateLimitError:
            errors["base"] = "rate_limited"
        except AuthError:
            errors["base"] = "invalid_code"
        except (AquaHomeConnectionError, ApiError):
            errors["base"] = "cannot_connect"
        else:
            return await self._async_probe_and_finish("verify")
        return self._async_show_form("verify", errors)

    async def _async_probe_and_finish(self, step_id: str) -> ConfigFlowResult:
        """Probe the account and finish the flow, mapping failures to ``step_id``.

        ``step_id`` names the form redrawn for a recoverable error (``user``,
        ``reauth_confirm``, or ``verify``). An unverified-account challenge routes
        to the verify step regardless of caller.
        """
        errors: dict[str, str] = {}
        try:
            outcome = await _async_probe_account(
                async_get_clientsession(self.hass),
                self._email,
                self._password,
                self.hass.config.language,
            )
        except UserNotVerifiedError:
            await self._async_request_verification_code(self._email)
            return await self.async_step_verify()
        except RateLimitError:
            errors["base"] = "rate_limited"
        except AuthError:
            errors["base"] = "invalid_auth"
        except (AquaHomeConnectionError, ApiError):
            errors["base"] = "cannot_connect"
        else:
            return await self._async_finish(outcome, step_id, errors)
        return self._async_show_form(step_id, errors)

    async def _async_finish(
        self, outcome: ProbeOutcome, step_id: str, errors: dict[str, str]
    ) -> ConfigFlowResult:
        """Create or update the config entry from a successful probe outcome."""
        login = outcome.login
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if login.user_id != entry.unique_id:
                errors["base"] = "wrong_account"
                return self._async_show_form(step_id, errors)
            # A reauth on a now-empty account must still succeed: stranding the
            # user on `no_devices` would leave the entry permanently broken.
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_PASSWORD: self._password,
                    CONF_HOST: outcome.host,
                    CONF_ACCESS_TOKEN: login.access_token,
                    CONF_REFRESH_TOKEN: login.refresh_token,
                },
            )
        if not outcome.devices:
            errors["base"] = "no_devices"
            return self._async_show_form(step_id, errors)
        await self.async_set_unique_id(login.user_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._email,
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_HOST: outcome.host,
                CONF_ACCESS_TOKEN: login.access_token,
                CONF_REFRESH_TOKEN: login.refresh_token,
            },
        )

    async def _async_request_verification_code(self, email: str) -> None:
        """Ask the cloud to email a fresh confirmation code (best effort).

        The challenge is account-level, so a throwaway :class:`AuthManager` on the
        legacy host suffices. A transient failure must not strand the user on the
        verify step: the login attempt itself typically triggers the code email,
        so the step proceeds regardless and a failure is only logged.
        """
        manager = AuthManager(async_get_clientsession(self.hass), base_url=API_BASE_URL)
        try:
            await manager.async_resend_confirmation_code(email)
        except AquaHomeError as err:
            _LOGGER.debug("Could not resend confirmation code: %s", err)

    def _async_show_form(
        self, step_id: str, errors: dict[str, str]
    ) -> ConfigFlowResult:
        """Render one of the flow's forms with ``errors`` and placeholders."""
        if step_id == "verify":
            return self.async_show_form(
                step_id="verify",
                data_schema=_VERIFY_SCHEMA,
                errors=errors,
                description_placeholders={CONF_EMAIL: self._email},
            )
        if step_id == "reauth_confirm":
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=_REAUTH_SCHEMA,
                errors=errors,
                description_placeholders={CONF_EMAIL: self._email},
            )
        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )
