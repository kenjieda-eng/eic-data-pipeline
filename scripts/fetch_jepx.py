"""
JEPX スポット価格の取得スクリプト。

JEPX のダウンロードは POST フォーム方式:
    POST https://www.jepx.jp/_download.php
    body: dir=spot_summary&file=spot_summary_YYYY.csv

ファイル名は年度ごとに決まっているので、年を総当たりして 200 が返ったものを採用する。

出力:
- data/raw/jepx/spot_summary_YYYY.csv         （生ファイル）
- data/processed/jepx/jepx-spot-{region}.csv  （共通スキーマ long 形式）
- data/processed/jepx/jepx-spot-{region}.parquet

デフォルト動作:
    python scripts/fetch_jepx.py
    → 直近 2 年分を取得

オプション:
    --years N         直近 N 年分を取得（デフォルト 2）
    --year YYYY       特定の 1 年だけ取得
    --all             2005 年以降の全年度を試す
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.http import post  # noqa: E402
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

# 元 CSV のエリア列 → 共通スキーマの region コード
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


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def download_year(
    post_url: str,
    dir_name: str,
    year: int,
    raw_dir: Path,
) -> pd.DataFrame:
    """
    POST で 1 年分の CSV を取得する。失敗時は空の DataFrame。
    """
    filename = f"spot_summary_{year}.csv"
    logger.info("POST %s dir=%s file=%s", post_url, dir_name, filename)
    resp = post(post_url, data={"dir": dir_name, "file": filename})

    if resp.status_code >= 400:
        logger.warning(
            "year=%d HTTP %d — skipping", year, resp.status_code
        )
        return pd.DataFrame()

    # JEPX はエラー時 HTML を返してくることがあるので、Content-Type や先頭バイトで判定
    ctype = resp.headers.get("Content-Type", "").lower()
    head = resp.content[:200]
    looks_like_html = (
        b"<html" in head.lower() or b"<!doctype" in head.lower()
        or "text/html" in ctype
    )
    if looks_like_html:
        logger.warning(
            "year=%d got HTML response (size=%d, ctype=%s) — treating as not found",
            year, len(resp.content), ctype,
        )
        return pd.DataFrame()
    if len(resp.content) < 200:
        logger.warning("year=%d response too small (%d bytes) — skipping",
                       year, len(resp.content))
        return pd.DataFrame()

    save_raw(resp.content, raw_dir, filename)

    for encoding in ("cp932", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(io.BytesIO(resp.content), encoding=encoding)
            logger.info(
                "parsed year=%d rows=%d encoding=%s",
                year, len(df), encoding,
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
    """JEPX 日次 CSV（48 コマ × 日数）を日次平均に集計して共通スキーマに。"""
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
            f"no value columns found. looked for system/area price columns. "
            f"actual columns={list(df.columns)}"
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


def years_to_fetch(args: argparse.Namespace) -> list[int]:
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    this_year = now_jst.year
    if args.year:
        return [args.year]
    if args.all:
        return list(range(2005, this_year + 1))
    return list(range(this_year - args.years + 1, this_year + 1))


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

    post_url: str = source_cfg["post_url"]
    dir_name: str = source_cfg["post_dir"]

    raw_dir = ROOT / "data" / "raw" / "jepx"
    processed_dir = ROOT / "data" / "processed" / "jepx"
    log_dir = ROOT / "data" / "_logs"

    target_years = years_to_fetch(args)
    logger.info("target years: %s", target_years)

    all_rows: list[pd.DataFrame] = []
    fetched_years: list[int] = []
    failed_years: list[int] = []

    for year in target_years:
        try:
            raw_df = download_year(post_url, dir_name, year, raw_dir)
        except Exception as e:
            logger.exception("fetch failed year=%d: %s", year, e)
            failed_years.append(year)
            continue
        if raw_df.empty:
            failed_years.append(year)
            continue
        try:
            source_url = (
                f"{post_url}?dir={dir_name}&file=spot_summary_{year}.csv"
            )
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
        write_processed(
            group,
            processed_dir,
            basename=str(indicator_id),
        )

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
