"""
JEPX スポット価格の取得スクリプト。

JEPX のダウンロードは:
    POST https://www.jepx.jp/_download.php
    body: dir=spot_summary&file=spot_summary_YYYY.csv
    要件: 市場ページを先に GET してセッションクッキー取得 + Referer ヘッダ

ファイル名リストは /js/spot_summary.js に書かれているので、まずそれを読む。

出力:
- data/raw/jepx/spot_summary_YYYY.csv         （生ファイル）
- data/processed/jepx/jepx-spot-{region}.csv  （共通スキーマ long 形式）
- data/processed/jepx/jepx-spot-{region}.parquet
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import time
from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml

# 連続取得時、JEPX サーバに配慮して挟むスリープ秒数。
# 通常の nightly は 2 年分だけなので影響ゼロ、--all の 20 年分で約 1 分追加。
SLEEP_BETWEEN_YEARS = 3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.http import (  # noqa: E402
    make_session,
    session_get,
    session_post,
)
from scripts.common.io import (  # noqa: E402
    append_log,
    save_raw,
    write_processed,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_jepx")

SOURCE_KEY = "jepx-spot"

AREA_MAP = {
    "エリアプライス北海道(円/kWh)": "hokkaido",
    "エリアプライス東北(円/kWh)": "tohoku",
    "エリアプライス東京(円/kWh)": "tokyo",
    "エリアプライス中部(円/kWh)": "chubu",
    "エリアプライス北陸(円/kWh)": "hokuriku",
    "エリアプライス関西(円/kWh)": "kansai",
    "エリアプライス中国(円/kWh)": "chugoku",
    "エリアプライス四国(円/kWh)": "shikoku",
    "エリアプライス九州(円/kWh)": "kyushu",
}
SYSTEM_COL_CANDIDATES = [
    "システムプライス(円/kWh)",
    "システムプライス（円/kWh）",
]
DATE_COL_CANDIDATES = ["受渡日", "年月日"]

# spot_summary.js にあるファイル名から年を拾う
YEAR_FROM_FILE_RE = re.compile(r"spot_summary_(\d{4})\.csv", re.IGNORECASE)
# spot_summary.js に書かれそうな候補（念のため広めにとる）
FILE_IN_JS_RE = re.compile(r"spot_summary_\d{4}[^\"'`\s]*\.csv", re.IGNORECASE)


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_js_filelist(
    session, js_url: str, raw_dir: Path,
) -> dict[int, str]:
    """spot_summary.js を取得してファイル名リストを抽出。"""
    logger.info("GET %s", js_url)
    r = session_get(session, js_url)
    r.raise_for_status()
    js_text = r.text

    # デバッグ保存
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "_spot_summary.js").write_text(js_text, encoding="utf-8")
    logger.info("saved JS (%d chars) to data/raw/jepx/_spot_summary.js", len(js_text))

    matches = FILE_IN_JS_RE.findall(js_text)
    if not matches:
        logger.warning("no filenames matched in spot_summary.js; first 500 chars:")
        logger.warning(js_text[:500])
        return {}

    year_to_file: dict[int, str] = {}
    for fname in matches:
        ym = YEAR_FROM_FILE_RE.search(fname)
        if ym:
            year_to_file[int(ym.group(1))] = fname

    logger.info(
        "found %d filename(s) in JS: %s",
        len(year_to_file),
        ", ".join(f"{y}={f}" for y, f in sorted(year_to_file.items())),
    )
    return year_to_file


def download_year(
    session,
    post_url: str,
    dir_name: str,
    filename: str,
    referer: str,
    raw_dir: Path,
    year: int,
) -> pd.DataFrame:
    """POST で 1 年分の CSV を取得。失敗時は空の DataFrame。"""
    logger.info("POST %s dir=%s file=%s", post_url, dir_name, filename)
    resp = session_post(
        session,
        post_url,
        data={"dir": dir_name, "file": filename},
        headers={
            "Referer": referer,
            "Origin": referer.rsplit("/electricpower", 1)[0],
            "Accept": "text/csv,application/csv,*/*",
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    if resp.status_code >= 400:
        logger.warning("year=%d HTTP %d — skipping", year, resp.status_code)
        return pd.DataFrame()

    ctype = resp.headers.get("Content-Type", "").lower()
    size = len(resp.content)
    head = resp.content[:200]
    looks_like_html = (
        b"<html" in head.lower() or b"<!doctype" in head.lower()
        or "text/html" in ctype
    )
    if looks_like_html or size < 200:
        # 応答ヘッダを詳しく残して診断しやすくする
        logger.warning(
            "year=%d non-CSV response: status=%d size=%d ctype=%s head=%r",
            year, resp.status_code, size, ctype, head[:120],
        )
        return pd.DataFrame()

    save_raw(resp.content, raw_dir, filename)

    for encoding in ("cp932", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(io.BytesIO(resp.content), encoding=encoding)
            logger.info(
                "parsed year=%d rows=%d encoding=%s", year, len(df), encoding,
            )
            return df
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"could not decode JEPX CSV for year={year}")


def _first_matching_col(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def normalize(df: pd.DataFrame, source_url: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]

    date_col = _first_matching_col(df, DATE_COL_CANDIDATES)
    if date_col is None:
        raise ValueError(
            f"date column not found. tried {DATE_COL_CANDIDATES}. "
            f"actual columns={list(df.columns)}"
        )
    system_col = _first_matching_col(df, SYSTEM_COL_CANDIDATES)
    present_area_cols = [c for c in AREA_MAP.keys() if c in df.columns]
    value_cols = ([system_col] if system_col else []) + present_area_cols
    if not value_cols:
        raise ValueError(
            f"no value columns found. actual columns={list(df.columns)}"
        )

    daily = df.groupby(date_col)[value_cols].mean().reset_index()

    rows: list[dict] = []
    for _, row in daily.iterrows():
        date = pd.to_datetime(row[date_col]).strftime("%Y-%m-%d")
        if system_col and pd.notna(row[system_col]):
            rows.append({
                "date": date,
                "indicator_id": "jepx-spot-system",
                "region": "jp",
                "value": float(row[system_col]),
                "source_url": source_url,
            })
        for area_col in present_area_cols:
            region = AREA_MAP[area_col]
            if pd.notna(row[area_col]):
                rows.append({
                    "date": date,
                    "indicator_id": f"jepx-spot-{region}",
                    "region": region,
                    "value": float(row[area_col]),
                    "source_url": source_url,
                })
    return pd.DataFrame(rows)


def pick_years(args, available: Iterable[int]) -> list[int]:
    available = sorted(set(available))
    if not available:
        return []
    if args.year:
        return [args.year] if args.year in available else []
    if args.all:
        return list(available)
    return available[-args.years:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch JEPX spot prices")
    parser.add_argument("--years", type=int, default=2)
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    market_page: str = source_cfg["market_page"]
    post_url: str = source_cfg["post_url"]
    dir_name: str = source_cfg["post_dir"]
    js_url: str = source_cfg["js_url"]

    raw_dir = ROOT / "data" / "raw" / "jepx"
    processed_dir = ROOT / "data" / "processed" / "jepx"
    log_dir = ROOT / "data" / "_logs"

    session = make_session()

    # 1) まず市場ページを GET してセッションクッキーを確立
    logger.info("priming session via %s", market_page)
    r = session_get(session, market_page)
    r.raise_for_status()
    logger.info("session cookies: %s", dict(session.cookies))

    # 2) JS からファイル名リストを抽出
    year_to_file: dict[int, str] = {}
    try:
        year_to_file = fetch_js_filelist(session, js_url, raw_dir)
    except Exception as e:
        logger.warning("JS fetch failed (%s) — falling back to year enumeration", e)

    if not year_to_file:
        # フォールバック: 年を総当たり（last 15 years）
        from datetime import datetime, timezone, timedelta
        now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
        for y in range(now_jst.year - 14, now_jst.year + 1):
            year_to_file[y] = f"spot_summary_{y}.csv"
        logger.info("fallback enumeration: years=%s", sorted(year_to_file))

    target_years = pick_years(args, year_to_file.keys())
    if not target_years:
        msg = f"no target years after filtering. available={sorted(year_to_file)}"
        logger.error(msg)
        append_log(log_dir, "fetch_jepx", "FAIL", msg)
        return 1
    logger.info("target years: %s", target_years)

    all_rows: list[pd.DataFrame] = []
    fetched_years: list[int] = []
    failed_years: list[int] = []

    for i, year in enumerate(target_years):
        if i > 0 and SLEEP_BETWEEN_YEARS > 0:
            logger.info("sleeping %ds before next year (server courtesy)", SLEEP_BETWEEN_YEARS)
            time.sleep(SLEEP_BETWEEN_YEARS)
        filename = year_to_file[year]
        try:
            raw_df = download_year(
                session, post_url, dir_name, filename,
                referer=market_page, raw_dir=raw_dir, year=year,
            )
        except Exception as e:
            logger.exception("fetch failed year=%d: %s", year, e)
            failed_years.append(year)
            continue
        if raw_df.empty:
            failed_years.append(year)
            continue
        try:
            source_url = f"{post_url}?dir={dir_name}&file={filename}"
            norm = normalize(raw_df, source_url=source_url)
        except Exception as e:
            logger.exception("normalize failed year=%d: %s", year, e)
            failed_years.append(year)
            continue
        all_rows.append(norm)
        fetched_years.append(year)

    if not all_rows:
        msg = f"no data fetched (failed_years={failed_years})"
        logger.error(msg)
        append_log(log_dir, "fetch_jepx", "FAIL", msg)
        return 1

    merged = pd.concat(all_rows, ignore_index=True)
    for indicator_id, group in merged.groupby("indicator_id"):
        write_processed(group, processed_dir, basename=str(indicator_id))

    summary = (
        f"years={fetched_years} rows={len(merged)} "
        f"range={merged['date'].min()}..{merged['date'].max()} "
        f"failed={failed_years}"
    )
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_jepx", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
