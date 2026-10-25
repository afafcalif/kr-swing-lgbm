from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import Settings, get_settings
from .errors import AuthError, ConfigError, HTTPError

TOKEN_EXPIRY_SKEW_SECONDS = 30


@dataclass
class TokenCache:
    path: Path

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def is_expired(self, expires_at: float | int | None) -> bool:
        if not expires_at:
            return True
        return time.time() >= float(expires_at) - TOKEN_EXPIRY_SKEW_SECONDS


class AuthManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cache = TokenCache(self.settings.token_cache_path)
        self.session = requests.Session()
        self.logger = logging.getLogger(self.__class__.__name__)

    def get_token(self, force: bool = False) -> str:
        cached = self.cache.load()
        if not force and cached:
            token = cached.get("access_token")
            expires_at = cached.get("expires_at")
            if token and not self.cache.is_expired(expires_at):
                return str(token)

        token, expires_at = self._request_token()
        self.cache.save({"access_token": token, "expires_at": expires_at})
        return token

    def revoke_token(self) -> None:
        if not self.settings.auth_revoke_path or not self.settings.auth_revoke_method:
            raise ConfigError(
                "Missing KIWOOM_AUTH_REVOKE_PATH/KIWOOM_AUTH_REVOKE_METHOD. "
                "Provide au10002 details from the official docs."
            )
        
        cached = self.cache.load()
        if not cached or not cached.get("access_token"):
            self.logger.warning("No cached token to revoke")
            return
        
        token = cached["access_token"]
        payload = {
            "appkey": self.settings.app_key,
            "secretkey": self.settings.app_secret,
            "token": token,
        }
        
        url = f"{self.settings.base_url.rstrip('/')}/{self.settings.auth_revoke_path.lstrip('/')}"
        self.logger.info("Revoking token: %s %s", self.settings.auth_revoke_method, url)
        
        response = self.session.request(
            self.settings.auth_revoke_method,
            url,
            json=payload,
            timeout=15,
        )
        
        if not response.ok:
            raise HTTPError(response.status_code, url, response.text[:500])
        
        self.cache.clear()
        self.logger.info("Token revoked successfully")

    def _request_token(self) -> tuple[str, float]:
        if not self.settings.auth_token_path or not self.settings.auth_token_method:
            raise ConfigError(
                "Missing KIWOOM_AUTH_TOKEN_PATH/KIWOOM_AUTH_TOKEN_METHOD. "
                "Provide au10001 details from the official docs."
            )
        payload = self._build_token_payload()
        url = f"{self.settings.base_url.rstrip('/')}/{self.settings.auth_token_path.lstrip('/')}"
        self.logger.info("Requesting auth token: %s %s", self.settings.auth_token_method, url)
        
        # Use requests.post() directly like reference code (not session.request())
        import requests
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
        }
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if not response.ok:
            raise HTTPError(response.status_code, url, response.text[:500])
        return self._parse_token_response(response.json())

    def _build_token_payload(self) -> dict[str, Any]:
        if not self.settings.app_key or not self.settings.app_secret:
            raise ConfigError("APP_KEY/APP_SECRET are required for token issuance.")
        return {
            "grant_type": "client_credentials",
            "appkey": self.settings.app_key,
            "secretkey": self.settings.app_secret,
        }

    def _parse_token_response(self, data: dict[str, Any]) -> tuple[str, float]:
        token = data.get("token")
        expires_dt = data.get("expires_dt")
        
        if not token:
            raise AuthError(f"Token not found in response: {data}")
        
        if not expires_dt:
            raise AuthError(f"expires_dt not found in response: {data}")
        
        # Parse expires_dt (format: YYYYMMDDHHMMSS) to Unix timestamp
        try:
            from datetime import datetime
            dt = datetime.strptime(str(expires_dt), "%Y%m%d%H%M%S")
            expires_at = dt.timestamp()
        except (ValueError, AttributeError) as e:
            raise AuthError(f"Failed to parse expires_dt '{expires_dt}': {e}")
        
        return token, expires_at
