class KiwoomError(Exception):
    """Base error for Kiwoom REST client."""


class ConfigError(KiwoomError):
    """Raised when required configuration is missing."""


class AuthError(KiwoomError):
    """Raised when authentication fails."""


class NotSent(KiwoomError):
    """요청을 보내지 않음 (유량 대기 상한 초과, 전송 직전 마감 시각 확인 실패). 서버에 닿지 않았으므로 다시 보내도 된다."""


class HTTPError(KiwoomError):
    """Raised for non-2xx HTTP responses."""

    def __init__(self, status_code: int, url: str, response_text: str) -> None:
        self.status_code = status_code
        self.url = url
        self.response_text = response_text
        super().__init__(self.__str__())

    def __str__(self) -> str:
        return f"HTTP {self.status_code} for {self.url}: {self.response_text}"
