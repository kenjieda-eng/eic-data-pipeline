"""
共通 HTTP ヘルパ。

- User-Agent を明示（一次ソース側で誰が叩いているか分かるように）
- 429 / 5xx は指数バックオフでリトライ
- タイムアウトは明示的に設定
- セッションを跨いでクッキーを保持するための make_session() を提供
- 依存は requests のみ

使い方（単発）:
    from scripts.common.http import get
    r = get("https://example.com/file.csv")
    r.raise_for_status()

使い方（セッション + Referer などが要るサイト）:
    from scripts.common.http import make_session, session_get, session_post

    s = make_session()
    session_get(s, "https://www.jepx.jp/electricpower/market-data/spot/")
    r = session_post(
        s,
        "https://www.jepx.jp/_download.php",
        data={"dir": "spot_summary", "file": "spot_summary_2024.csv"},
        headers={"Referer": "https://www.jepx.jp/electricpower/market-data/spot/"},
    )
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "eic-data-pipeline/0.1 "
    "(+https://github.com/kenjieda-eng/eic-data-pipeline; "
    "contact: kenji.eda@gmail.com)"
)

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF = 2.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def make_session() -> requests.Session:
    """クッキーを保持するセッションを作る。"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "ja,en;q=0.8",
    })
    return s


def _retry_loop(
    send,
    url: str,
    max_retries: int,
    backoff: float,
) -> requests.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            resp = send()
        except requests.RequestException as e:
            last_exc = e
            if attempt >= max_retries:
                raise
            wait = backoff ** attempt
            logger.warning(
                "HTTP error on %s (attempt %d/%d): %s — retrying in %.1fs",
                url, attempt + 1, max_retries + 1, e, wait,
            )
            time.sleep(wait)
            continue

        if resp.status_code in RETRYABLE_STATUS and attempt < max_retries:
            wait = backoff ** attempt
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = max(wait, float(retry_after))
            logger.warning(
                "HTTP %d on %s (attempt %d/%d) — retrying in %.1fs",
                resp.status_code, url, attempt + 1, max_retries + 1, wait,
            )
            time.sleep(wait)
            continue

        return resp

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"unreachable retry loop exit for {url}")


def get(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> requests.Response:
    """単発 GET（セッション不要のケース）。"""
    merged_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged_headers.update(headers)

    return _retry_loop(
        lambda: requests.get(url, params=params, headers=merged_headers, timeout=timeout),
        url, max_retries, backoff,
    )


def post(
    url: str,
    *,
    data: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> requests.Response:
    """単発 POST。"""
    merged_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged_headers.update(headers)

    return _retry_loop(
        lambda: requests.post(url, data=data, headers=merged_headers, timeout=timeout),
        url, max_retries, backoff,
    )


def session_get(
    session: requests.Session,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> requests.Response:
    """セッション GET（クッキー保持）。"""
    return _retry_loop(
        lambda: session.get(url, params=params, headers=headers, timeout=timeout),
        url, max_retries, backoff,
    )


def session_post(
    session: requests.Session,
    url: str,
    *,
    data: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> requests.Response:
    """セッション POST（クッキー保持）。"""
    return _retry_loop(
        lambda: session.post(url, data=data, headers=headers, timeout=timeout),
        url, max_retries, backoff,
    )
