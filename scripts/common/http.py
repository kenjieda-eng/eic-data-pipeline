"""
共通 HTTP ヘルパ。

- User-Agent を明示（一次ソース側で誰が叩いているか分かるように）
- 429 / 5xx は指数バックオフでリトライ
- タイムアウトは明示的に設定
- 依存は requests のみ

使い方:
    from scripts.common.http import get

    r = get("https://www.jepx.jp/js/csv/spot_summary_2024.csv")
    r.raise_for_status()
    csv_bytes = r.content
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# このリポジトリが誰なのか、一次ソース側から見えるように名乗る。
USER_AGENT = (
    "eic-data-pipeline/0.1 "
    "(+https://github.com/kenjieda-eng/eic-data-pipeline; "
    "contact: kenji.eda@gmail.com)"
)

DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF = 2.0  # seconds, exponential base
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def get(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> requests.Response:
    """
    GET with retry. 429 / 5xx は最大 `max_retries` 回まで指数バックオフで再試行。
    それ以外のエラーは即座に raise_for_status 対象として呼び出し元に返す。
    """
    merged_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged_headers.update(headers)

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=timeout,
            )
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
            # Retry-After があれば優先
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

    # ここには通常来ないが安全網として
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"unreachable retry loop exit for {url}")
