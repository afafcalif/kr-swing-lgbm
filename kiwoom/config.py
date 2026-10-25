from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError

load_dotenv()


def _get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None or value == "":
        return None
    return value


@dataclass(frozen=True)
class Settings:
    env: str
    base_url: str
    app_key: str | None
    app_secret: str | None
    account_no: str | None
    token_cache_path: Path
    auth_token_path: str | None
    auth_token_method: str | None
    auth_revoke_path: str | None
    auth_revoke_method: str | None
    ws_url: str
    commission_rate: float
    tax_rate: float

    @classmethod
    def from_env(cls, env: str | None = None) -> "Settings":
        """env 를 주면 KIWOOM_ENV 대신 그 서버 설정을 만든다 (예: 시세 수집은 항상 "prod")."""
        env = (env or _get_env("KIWOOM_ENV", "mock") or "mock").lower()
        if env not in {"mock", "prod"}:
            raise ConfigError("KIWOOM_ENV must be 'mock' or 'prod'.")

        base_url = {
            "mock": _get_env("KIWOOM_MOCK_URL", "http://127.0.0.1:9443"),
            "prod": "https://api.kiwoom.com",
        }[env]

        # 수수료율 (매수/매도 각각 적용)
        # 사용자의 요청에 따라 모의투자(Mock) 시에도 실전투자(Prod) 기준인 0.015% 적용
        commission_rate = 0.00015
        
        # 제세금 (매도시에만 적용)
        # ETF는 증권거래세 면제 (0.0%). Stock은 0.23%
        # 전략이 ETF 전용이므로 0.0으로 설정.
        tax_rate = 0.0

        # 토큰은 서버마다 따로 저장한다 (실서버 토큰을 모의 서버 요청에 다시 쓰지 않게)
        cache_base = Path(
            _get_env("KIWOOM_TOKEN_CACHE_PATH", ".kiwoom_token_cache.json")
            or ".kiwoom_token_cache.json"
        ).expanduser()
        token_cache_path = cache_base.with_name(f"{cache_base.stem}.{env}{cache_base.suffix}")

        # 모의투자는 모의투자 전용 키(MOCK_*)를 쓴다. 없으면 예전처럼 APP_* 를 쓴다.
        key_prefix = "MOCK_" if env == "mock" and _get_env("MOCK_APP_KEY") else ""

        return cls(
            env=env,
            base_url=base_url,
            app_key=_get_env(f"{key_prefix}APP_KEY"),
            app_secret=_get_env(f"{key_prefix}APP_SECRET"),
            account_no=_get_env(f"{key_prefix}ACCOUNT_NO"),
            token_cache_path=token_cache_path,
            auth_token_path=_get_env("KIWOOM_AUTH_TOKEN_PATH", "/oauth2/token"),
            auth_token_method=_get_env("KIWOOM_AUTH_TOKEN_METHOD", "POST"),
            auth_revoke_path=_get_env("KIWOOM_AUTH_REVOKE_PATH", "/oauth2/revoke"),
            auth_revoke_method=_get_env("KIWOOM_AUTH_REVOKE_METHOD", "POST"),
            ws_url={
                "mock": "wss://mockapi.kiwoom.com:10000/api/dostk/websocket",
                "prod": "wss://api.kiwoom.com:10000/api/dostk/websocket",
            }[env],
            commission_rate=commission_rate,
            tax_rate=tax_rate
        )


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings.from_env()
    return _SETTINGS
