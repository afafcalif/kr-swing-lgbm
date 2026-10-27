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
REFRESH_AHEAD_SECONDS = 1800   # 만료 30분 전부터 미리 갱신 (실패하면 기존 토큰으로 계속)
RETRY_AFTER_FAIL_SECONDS = 60  # 미리 갱신·발급이 실패하면 이 시간 동안은 다시 발급하지 않고 기존 토큰 (요청마다 기다리지 않게)


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

    def is_expired(self, expires_at: float | int | None, skew: float = TOKEN_EXPIRY_SKEW_SECONDS) -> bool:
        if not expires_at:
            return True
        return time.time() >= float(expires_at) - skew


class AuthManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cache = TokenCache(self.settings.token_cache_path)
        self.session = requests.Session()
        self.logger = logging.getLogger(self.__class__.__name__)
        self._next_try = 0.0

    def get_token(self, force: bool = False, timeout: float | None = None, ahead: float | None = None) -> str:
        """토큰. 만료 ahead 초(기본 30분) 전부터 미리 다시 발급한다. 발급이 실패하면(timeout 초 제한) 실제 만료 시각 전인 기존 토큰만
        돌려주고 RETRY_AFTER_FAIL_SECONDS 동안은 다시 발급하지 않는다. 기존 토큰이 만료됐거나 없거나 force 면 시도마다 다시 발급하고
        실패를 그대로 올린다 (만료 토큰은 쓰지 않음, 60초 억제도 없음)"""
        cached = self.cache.load() or {}
        token, expires_at = cached.get("access_token"), cached.get("expires_at")
        live = bool(token) and time.time() < float(expires_at or 0)  # 실제 만료 시각 전
        if not force and live:
            if not self.cache.is_expired(expires_at, REFRESH_AHEAD_SECONDS if ahead is None else ahead):
                return str(token)
            if time.time() < self._next_try:
                return str(token)
        try:
            new, new_exp = self._request_token(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            if force or not live:
                raise
            self._next_try = time.time() + RETRY_AFTER_FAIL_SECONDS
            self.logger.warning("토큰 미리 갱신 실패 -> 기존 토큰으로 계속 (만료까지 %.0f초): %r", float(expires_at or 0) - time.time(), e)
            return str(token)
        self.cache.save({"access_token": new, "expires_at": new_exp})
        self._next_try = 0.0
        return new

    def invalidate(self) -> None:
        """서버가 인증·토큰으로 거부했을 때: 캐시 토큰을 버려 다음 요청에서 다시 발급"""
        self.cache.clear()
        self._next_try = 0.0

    def seconds_left(self) -> float:
        """캐시된 토큰의 만료까지 남은 초 (없으면 0)"""
        cached = self.cache.load() or {}
        return max(float(cached.get("expires_at") or 0) - time.time(), 0.0) if cached.get("access_token") else 0.0

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

    def _request_token(self, timeout: float | None = None) -> tuple[str, float]:
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
        response = requests.post(url, headers=headers, json=payload, timeout=timeout or 30)
        
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
