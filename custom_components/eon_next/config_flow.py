"""Config flow to configure Eon Next."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_BACKFILL_CHUNK_DAYS,
    CONF_BACKFILL_DELAY_SECONDS,
    CONF_BACKFILL_ENABLED,
    CONF_BACKFILL_LOOKBACK_DAYS,
    CONF_BACKFILL_REBUILD_STATISTICS,
    CONF_BACKFILL_REQUESTS_PER_RUN,
    CONF_BACKFILL_RUN_INTERVAL_MINUTES,
    CONF_API_KEY,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SHOW_CARD,
    CONF_SHOW_PANEL,
    DEFAULT_BACKFILL_CHUNK_DAYS,
    DEFAULT_BACKFILL_DELAY_SECONDS,
    DEFAULT_BACKFILL_ENABLED,
    DEFAULT_BACKFILL_LOOKBACK_DAYS,
    DEFAULT_BACKFILL_REBUILD_STATISTICS,
    DEFAULT_BACKFILL_REQUESTS_PER_RUN,
    DEFAULT_BACKFILL_RUN_INTERVAL_MINUTES,
    DEFAULT_SHOW_CARD,
    DEFAULT_SHOW_PANEL,
    DOMAIN,
)
from .eonnext import EonNext, EonNextApiError, redact_secrets

_LOGGER = logging.getLogger(__name__)


class EonNextConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Eon Next config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EonNextOptionsFlow:
        """Return options flow for this handler."""
        return EonNextOptionsFlow()

    async def _validate_credentials(self, api_key: str) -> str | None:
        """Validate an API key against E.ON Next.

        Returns the refresh token on success, or None if the key is rejected.
        """
        api = EonNext()
        try:
            if await api.login_with_api_key(api_key, initialise=False):
                return api.auth["refresh"]["token"]
            return None
        except EonNextApiError as err:
            _LOGGER.debug(
                "API/connection error during credential validation: %s",
                redact_secrets(err),
            )
            raise
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected error during authentication")
            raise
        finally:
            await api.async_close()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Invoked when a user initiates a flow via the user interface."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            api_key = (user_input.get(CONF_API_KEY) or "").strip()

            await self.async_set_unique_id(email)
            self._abort_if_unique_id_configured()

            if not api_key:
                errors["base"] = "missing_credentials"
            else:
                try:
                    refresh_token = await self._validate_credentials(api_key)
                except EonNextApiError:
                    errors["base"] = "cannot_connect"
                except Exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                else:
                    if refresh_token is not None:
                        return self.async_create_entry(
                            title="Eon Next",
                            data={
                                CONF_EMAIL: email,
                                CONF_API_KEY: api_key,
                                CONF_REFRESH_TOKEN: refresh_token,
                            },
                        )
                    errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(  # pyright: ignore[reportArgumentType]
                {
                    vol.Required(CONF_EMAIL): cv.string,
                    vol.Required(CONF_API_KEY): cv.string,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        """Handle initiation of re-auth flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ):
        """Handle confirmation of re-auth flow."""
        errors: dict[str, str] = {}

        reauth_entry = self._get_reauth_entry()
        existing_email = reauth_entry.data[CONF_EMAIL]

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            api_key = (user_input.get(CONF_API_KEY) or "").strip()

            # Reauth must stay on the same account: if the user submits a
            # different email, abort instead of silently rebinding the entry to
            # another account (which would leave unique_id pointing at A while
            # its data belongs to B - duplicate entries and colliding
            # meter-serial unique_ids follow).
            await self.async_set_unique_id(email)
            self._abort_if_unique_id_mismatch(reason="reauth_account_mismatch")

            if not api_key:
                errors["base"] = "missing_credentials"
            else:
                try:
                    refresh_token = await self._validate_credentials(api_key)
                except EonNextApiError:
                    errors["base"] = "cannot_connect"
                except Exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                else:
                    if refresh_token is not None:
                        return self.async_update_reload_and_abort(
                            reauth_entry,
                            data_updates={
                                CONF_EMAIL: email,
                                CONF_API_KEY: api_key,
                                CONF_REFRESH_TOKEN: refresh_token,
                                # Scrub any password stored by older versions;
                                # E.ON no longer accepts it and it should not
                                # linger in the config entry.
                                CONF_PASSWORD: "",
                            },
                            reason="reauth_successful",
                        )
                    errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(  # pyright: ignore[reportArgumentType]
                {
                    vol.Required(CONF_EMAIL, default=existing_email): cv.string,
                    vol.Required(CONF_API_KEY): cv.string,
                }
            ),
            errors=errors,
        )


class EonNextOptionsFlow(config_entries.OptionsFlow):
    """Handle Eon Next options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage Eon Next options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(  # pyright: ignore[reportArgumentType]
                {
                    vol.Required(
                        CONF_SHOW_PANEL,
                        default=options.get(
                            CONF_SHOW_PANEL, DEFAULT_SHOW_PANEL
                        ),
                    ): bool,
                    vol.Required(
                        CONF_SHOW_CARD,
                        default=options.get(
                            CONF_SHOW_CARD, DEFAULT_SHOW_CARD
                        ),
                    ): bool,
                    vol.Required(
                        CONF_BACKFILL_ENABLED,
                        default=options.get(
                            CONF_BACKFILL_ENABLED, DEFAULT_BACKFILL_ENABLED
                        ),
                    ): bool,
                    vol.Required(
                        CONF_BACKFILL_REBUILD_STATISTICS,
                        default=options.get(
                            CONF_BACKFILL_REBUILD_STATISTICS,
                            DEFAULT_BACKFILL_REBUILD_STATISTICS,
                        ),
                    ): bool,
                    vol.Required(
                        CONF_BACKFILL_LOOKBACK_DAYS,
                        default=options.get(
                            CONF_BACKFILL_LOOKBACK_DAYS,
                            DEFAULT_BACKFILL_LOOKBACK_DAYS,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=36500)),
                    vol.Required(
                        CONF_BACKFILL_CHUNK_DAYS,
                        default=options.get(
                            CONF_BACKFILL_CHUNK_DAYS,
                            DEFAULT_BACKFILL_CHUNK_DAYS,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=31)),
                    vol.Required(
                        CONF_BACKFILL_REQUESTS_PER_RUN,
                        default=options.get(
                            CONF_BACKFILL_REQUESTS_PER_RUN,
                            DEFAULT_BACKFILL_REQUESTS_PER_RUN,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
                    vol.Required(
                        CONF_BACKFILL_RUN_INTERVAL_MINUTES,
                        default=options.get(
                            CONF_BACKFILL_RUN_INTERVAL_MINUTES,
                            DEFAULT_BACKFILL_RUN_INTERVAL_MINUTES,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                    vol.Required(
                        CONF_BACKFILL_DELAY_SECONDS,
                        default=options.get(
                            CONF_BACKFILL_DELAY_SECONDS,
                            DEFAULT_BACKFILL_DELAY_SECONDS,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                }
            ),
        )
