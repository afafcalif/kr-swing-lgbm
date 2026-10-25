"""텔레그램 알림 (.env 의 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). 값이 없으면 로그에만 남긴다. 토큰은 로그에 쓰지 않는다."""
import os
import time

import pandas as pd


def _env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))
    except Exception:  # noqa: BLE001
        pass
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")


def send(text, log_path=None, sender=None):
    """반환: 보냈으면 True. sender 는 시험용 (기본은 텔레그램 HTTP)"""
    line = f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S} {text}"
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    token, chat = _env()
    if sender is not None:
        return sender(text)
    if not token or not chat:
        return False
    import requests
    ok = False
    for i in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": text}, timeout=10)
            if r.status_code == 200:
                ok = True
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2 * (i + 1))
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S}   -> 텔레그램 {'보냄' if ok else '보내지 못함'}\n")
    return ok
