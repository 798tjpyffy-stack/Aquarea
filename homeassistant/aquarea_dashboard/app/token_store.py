"""Persist Aquarea OAuth tokens and renew them via the refresh token.

The password is never written to disk. A restored client (no password) can only
renew via the refresh token; if that fails the user has to log in again.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

import aiohttp

from aioaquarea import ApiError, AquareaEnvironment, AuthenticationError, Client
from aioaquarea.auth import PanasonicRequestHeader
from aioaquarea.const import APP_CLIENT_ID, AUTH_0_CLIENT, AUTH_API_USER_AGENT, BASE_PATH_AUTH
from aioaquarea.errors import AuthenticationErrorCodes

_LOGGER = logging.getLogger("aquarea_token")

RENEW_MARGIN = dt.timedelta(minutes=15)
_NO_PASSWORD = "\x00no-password\x00"  # constructor requires one; never sent (login() is overridden)
TOKEN_FIELDS = ("access_token", "refresh_token", "expires_at", "scope", "client_id")
_VOLATILE_HEADERS = (
    "x-app-version",
    "x-app-timestamp",
    "x-cfc-api-key",
    "x-user-authorization-v2",
    "x-client-id",
)


class PersistentClient(Client):
    """aioaquarea client that renews via refresh token and reports new tokens."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str | None = None):
        super().__init__(
            session=session,
            username=username,
            password=password or _NO_PASSWORD,
            refresh_login=True,
            environment=AquareaEnvironment.PRODUCTION,
        )
        self._has_password = password is not None
        self._app_version_ready = False
        self.on_token: Callable[[], None] | None = None
        self._wrap_requests()

    def _wrap_requests(self) -> None:
        """Retry each API request once after renewing the token / app version.

        aioaquarea swallows errors inside status/consumption calls, so retrying
        at a higher level would never see them.
        """
        original = self._api_client.request

        async def fresh_headers(kwargs: dict[str, Any]) -> None:
            # Callers pre-build headers (token, app version, api key) before
            # calling request(); update those so renewals take effect.
            headers = kwargs.get("headers")
            if not headers or not self._settings.access_token:
                return
            current = await PanasonicRequestHeader.get(self._settings, self._app_version)
            for key in _VOLATILE_HEADERS:
                if key in headers and key in current:
                    headers[key] = current[key]

        async def request(*args: Any, **kwargs: Any) -> aiohttp.ClientResponse:
            await self._ensure_app_version()
            await fresh_headers(kwargs)
            try:
                return await original(*args, **kwargs)
            except AuthenticationError as ex:
                if ex.error_code != AuthenticationErrorCodes.TOKEN_EXPIRED:
                    raise
                _LOGGER.info("Token rejected for %s, renewing", self.username)
                await self.login()
            except ApiError as ex:
                if "new version" not in str(ex.error_message).lower():
                    raise
                _LOGGER.info("Panasonic requires a newer app version, refreshing")
                await self._app_version.refresh()
                # The ACC clientId is bound to the app version.
                await self._authenticator._retrieve_client_acc()  # noqa: SLF001
                if self.on_token:
                    self.on_token()
            await fresh_headers(kwargs)
            return await original(*args, **kwargs)

        self._api_client.request = request  # type: ignore[method-assign]

    async def _ensure_app_version(self) -> None:
        if not self._app_version_ready:
            self._app_version_ready = True
            await self._app_version.init()

    # ---------------------------------------------------------------- state
    def export_token(self) -> dict[str, Any]:
        s = self._settings
        return {
            "access_token": s.access_token,
            "refresh_token": s.refresh_token,
            "expires_at": s.expires_at,
            "scope": s.scope,
            "client_id": s.clientId,
        }

    def restore_token(self, data: dict[str, Any]) -> None:
        s = self._settings
        s.set_token(data.get("access_token"), data.get("refresh_token"), data.get("expires_at"), data.get("scope"))
        s.clientId = data.get("client_id")
        self._apply_token()

    def _apply_token(self) -> None:
        self._api_client.access_token = self._settings.access_token
        expires = self._settings.expires_at
        self._api_client.token_expiration = (
            dt.datetime.fromtimestamp(expires, tz=dt.timezone.utc) if expires else None
        )

    @property
    def needs_renewal(self) -> bool:
        exp = self._api_client.token_expiration
        if not self._api_client.access_token or not self._settings.clientId:
            return True
        return exp is not None and dt.datetime.now(dt.timezone.utc) + RENEW_MARGIN >= exp

    async def ensure_token(self) -> None:
        """Renew the token if it is missing or expires soon."""
        if self.needs_renewal:
            await self.login()

    # ---------------------------------------------------------------- login
    async def login(self) -> None:
        """Renew via refresh token; fall back to password login if available."""
        intent = dt.datetime.now()
        async with self._login_lock:
            if self._last_login > intent:
                return  # someone else renewed meanwhile
            await self._ensure_app_version()

            if self._settings.refresh_token:
                try:
                    await self._refresh()
                    self._token_changed()
                    _LOGGER.info("Token renewed for %s", self.username)
                    return
                except (AuthenticationError, aiohttp.ClientError, KeyError, TypeError, ValueError) as ex:
                    _LOGGER.warning("Token refresh failed for %s: %s", self.username, ex)

            if not self._has_password:
                raise AuthenticationError(
                    AuthenticationErrorCodes.TOKEN_EXPIRED,
                    "Stored token could not be renewed. Please log in again.",
                )
            await self._authenticator.authenticate(self.username, self.password)
            self._token_changed()

    async def _refresh(self) -> None:
        # aioaquarea's own refresh_token() omits the refresh token, so do it here.
        received = time.time()
        async with self._sess.post(
            f"{BASE_PATH_AUTH}/oauth/token",
            headers={"Auth0-Client": AUTH_0_CLIENT, "user-agent": AUTH_API_USER_AGENT},
            json={
                "scope": self._settings.scope,
                "client_id": APP_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self._settings.refresh_token,
            },
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                raise AuthenticationError(
                    AuthenticationErrorCodes.API_ERROR, f"Token refresh: HTTP {resp.status}"
                )
            data = await resp.json(content_type=None)
        self._settings.set_token(
            data["access_token"],
            data.get("refresh_token") or self._settings.refresh_token,  # rotation is optional
            received + data["expires_in"],
            data.get("scope") or self._settings.scope,
        )
        # Same as a fresh login: register the new token and get the ACC clientId.
        await self._authenticator._retrieve_client_acc()  # noqa: SLF001

    def _token_changed(self) -> None:
        self._last_login = dt.datetime.now()
        self._apply_token()
        if self.on_token:
            self.on_token()


class TokenStore:
    """JSON file (mode 600) with tokens per account; no passwords."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as ex:
            _LOGGER.error("Could not read token store %s: %s", self.path, ex)
            return {}
        accounts = data.get("accounts") if isinstance(data, dict) else None
        return {k: v for k, v in (accounts or {}).items() if isinstance(v, dict)}

    def save(self, username: str, entry: dict[str, Any]) -> None:
        accounts = self.load()
        accounts[username] = entry
        self._write(accounts)

    def remove(self, username: str) -> None:
        accounts = self.load()
        if accounts.pop(username, None) is not None:
            self._write(accounts)

    def _write(self, accounts: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"accounts": accounts}, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
