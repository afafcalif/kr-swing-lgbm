from __future__ import annotations

import logging
from typing import Any

import requests

from .auth import AuthManager
from .config import Settings, get_settings
from .errors import AuthError, HTTPError, NotSent


import time
from threading import Lock

class RateLimiter:
    """초당 요청 횟수 제한 (Thread-safe)"""
    def __init__(self, limits_per_sec: int = 4):
        self.interval = 1.0 / limits_per_sec
        self.last_call = 0.0
        self.lock = Lock()

    def wait(self, max_wait=None):
        """다음 호출까지 기다린다. max_wait(초)를 주고 기다릴 시간이 그보다 길면 기다리지 않고 False (호출 기록도 안 남김)"""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_call
            wait_time = self.interval - elapsed
            if max_wait is not None and wait_time > max_wait:
                return False
            if wait_time > 0:
                time.sleep(wait_time)
            self.last_call = time.time()
            return True

class KiwoomRestClient:
    def __init__(
        self,
        settings: Settings | None = None,
        auth: AuthManager | None = None,
        timeout: int = 15,
    ) -> None:
        self.settings = settings or get_settings()
        self.auth = auth or AuthManager(self.settings)
        self.timeout = timeout
        self.session = requests.Session()
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # Rate Limiter 추가 (Mock Server 안정성을 위해 1회/초로 보수적 설정)
        self.rate_limiter = RateLimiter(limits_per_sec=1)
        # send_gate(tr_code, max_wait) -> True/False: 있으면 rate_limiter 대신 이것 하나로 기다린다 (실행 코드의 프로세스 공용 제한기, 11L 보완).
        # 토큰 준비 뒤 HTTP 전송 바로 앞에서 불러, 기다린 뒤의 실제 전송 시각이 간격 기준이 된다
        self.send_gate = None

    def request(
        self,
        tr_code: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        cont_yn: str = "N",
        next_key: str = "",
        retry_429: bool = True,
        max_wait: float | None = None,
        before_send=None,
        timeout: float | None = None,
    ) -> requests.Response:
        # 요청 재시도 로직 (429 대응). retry_429=False 면 429 를 받는 즉시 None 을 돌려준다
        # (주문 TR: 재시도는 호출하는 쪽이 마감 시각을 확인하며 한다, 2026-10-24 Codex 확인 리뷰 26)
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:  # 손절 감시 안 토큰 발급도 같은 제한. 만료 전 기존 토큰은 발급 실패에도 씀, 그 밖의 발급 실패 = 보내지 않음 (AuthError)
                token = self.auth.get_token(**({"timeout": timeout} if timeout is not None else {}))
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                raise AuthError(f"토큰 발급 실패 - 보내지 않음 (api-id={tr_code}): {e!r}"[:300]) from e
            # 요청 전 대기: 토큰 준비 뒤, 전송 바로 앞 (기다린 뒤 바로 보냄 = 실제 전송 간격). 대기 장치는 하나 (send_gate 가 있으면 그것만)
            gate = getattr(self, "send_gate", None)
            if gate is not None:
                if not gate(tr_code, max_wait):
                    raise NotSent(f"유량 대기 상한 {max_wait}초 초과 (api-id={tr_code}) - 보내지 않음")
            elif max_wait is None:
                self.rate_limiter.wait()
            elif self.rate_limiter.wait(max_wait=max_wait) is False:
                raise NotSent(f"유량 대기 상한 {max_wait}초 초과 (api-id={tr_code}) - 보내지 않음")
            # before_send: 유량 대기·토큰 뒤 보내기 바로 앞 확인 (주문 TR 마감 시각, 2026-10-24 Codex 2차 확인 리뷰 4), False 면 NotSent
            if before_send is not None and not before_send():
                raise NotSent(f"전송 직전 확인 실패: 마감 시각 지남 (api-id={tr_code}) - 보내지 않음")
            url = f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"
            headers = {
                "Authorization": f"Bearer {token}",
                "api-id": tr_code,
                "cont-yn": cont_yn,
                "next-key": next_key,
            }
            self.logger.info("%s %s (api-id=%s) [Attempt %d]", method, url, tr_code, attempt + 1)
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
                timeout=self.timeout if timeout is None else timeout,  # timeout: 이 요청만의 시간 제한 (손절 감시 5초)
            )
            
            # 429 Too Many Requests 처리
            if response.status_code == 429:
                if not retry_429:
                    self.logger.warning("HTTP 429 Rate Limit (재시도 안 함, api-id=%s)", tr_code)
                    return None
                self.logger.warning(f"HTTP 429 Rate Limit. Retrying in {2**(attempt+1)}s...")
                time.sleep(2 ** (attempt + 1))
                continue
                
            if not response.ok:
                raise HTTPError(response.status_code, url, response.text[:500])
                
            return response

    def request_all_pages(
        self,
        tr_code: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        cont_yn: str = "N",
        next_key: str = "",
        max_pages: int = 50,
    ) -> list[requests.Response]:
        pages: list[requests.Response] = []
        current_cont = cont_yn
        current_next_key = next_key
        for _ in range(max_pages):
            response = self.request(
                tr_code=tr_code,
                method=method,
                path=path,
                params=params,
                json=json,
                cont_yn=current_cont,
                next_key=current_next_key,
            )
            pages.append(response)
            header_next_key = response.headers.get("next-key")
            header_cont_yn = response.headers.get("cont-yn")
            if not header_next_key or header_cont_yn != "Y":
                break
            current_cont = "Y"
            current_next_key = header_next_key
        return pages
