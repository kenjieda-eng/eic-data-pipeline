"""
JEPX スポット価格の取得スクリプト。

- 一次ソース: https://www.jepx.jp/js/csv/spot_summary_{YYYY}.csv
- 出力: data/raw/jepx/spot_summary_YYYY.csv （生ファイル）
        data/processed/jepx/jepx-spot-{region}.csv / .parquet （共通スキーマ）

デフォルト動作:
    python scripts/fetch_jepx.py
    → 当該年と前年の 2 ファイルを取得して processed を更新

オプション:
    --years N         直近 N 年分を取得（デフォルト 2）
    --year YYYY       特定の 1 年だけ取得

共通スキーマ (date, indicator_id, region, value, source_url) に変換し、
日次平均（48 コマの単純平均）を算出する。
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

# プロジェクトルートを sys.path に追加（scripts/ から scripts.common を import するため）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.http import get  # noqa: E402
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
SYSTEM_COL = "システムプライス(円/kWh)"
DATE_COL = "受渡日"


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_year(year: int, url_template: str, raw_dir: Path) -> pd.DataFrame:
    """指定年の CSV を取得して DataFrame で返す。raw も保存。"""
    url = url_template.format(year=year)
    logger.info("fetching %s", url)
    resp = get(url)
    if resp.status_code == 404:
        logger.warning("year=%d not found (404) — skipping", year)
        return pd.DataFrame()
    resp.raise_for_status()

    save_raw(resp.content, raw_dir, f"spot_summary_{year}.csv")

    # JEPX CSV は Shift_JIS。列名に日本語が含まれる。
    for encoding in ("cp932", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(io.BytesIO(resp.content), encoding=encoding)
            logger.info("parsed year=%d rows=%d encoding=%s", year, len(df), encoding)
            return df
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"could not decode JEPX CSV for year={year}")


def normalize(df: pd.DataFrame, source_url: str) -> pd.DataFrame:
    """
    JEPX 日次 CSV（48 コマ × 日数）を日次平均に集計して共通スキーマにする。
    システムプライス + 9 エリアプライス を出力。
    """
    if df.empty:
        return df

    # 列名の空白を除去
    df.columns = [c.strip() for c in df.columns]

    if DATE_COL not in df.columns:
        raise ValueError(f"expected column {DATE_COL!r} not found. columns={list(df.columns)}")

    # 日次平均
    value_cols = [SYSTEM_COL] + list(AREA_MAP.keys())
    present = [c for c in value_cols if c in df.columns]
    daily = df.groupby(DATE_COL)[present].mean().reset_index()

    rows: list[dict] = []
    for _, row in daily.iterrows():
        date = pd.to_datetime(row[DATE_COL]).strftime("%Y-%m-%d")
        if SYSTEM_COL in present and pd.notna(row[SYSTEM_COL]):
            rows.append({
                "date": date,
                "indicator_id": "jepx-spot-system",
                "region": "jp",
                "value": float(row[SYSTEM_COL]),
                "source_url": source_url,
            })
        for area_col, region in AREA_MAP.items():
            if area_col in present and pd.notna(row[area_col]):
                rows.append({
                    "date": date,
                    "indicator_id": f"jepx-spot-{region}",
                    "region": region,
                    "value": float(row[area_col]),
                    "source_url": source_url,
                })

    return pd.DataFrame(rows)


def years_to_fetch(args: argparse.Namespace) -> Iterable[int]:
    # JST の "今年"
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    this_year = now_jst.year
    if args.year:
        return [args.year]
    return list(range(this_year - args.years + 1, this_year + 1))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch JEPX spot prices")
    parser.add_argument("--years", type=int, default=2,
                        help="直近 N 年分を取得（デフォルト 2）")
    parser.add_argument("--year", type=int, default=None,
                        help="特定の 1 年だけ取得（--years より優先）")
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2
    url_template: str = source_cfg["url_template"]

    raw_dir = ROOT / "data" / "raw" / "jepx"
    processed_dir = ROOT / "data" / "processed" / "jepx"
    log_dir = ROOT / "data" / "_logs"

    all_rows: list[pd.DataFrame] = []
    fetched_years: list[int] = []
    failed_years: list[int] = []

    for year in years_to_fetch(args):
        source_url = url_template.format(year=year)
        try:
            raw_df = fetch_year(year, url_template, raw_dir)
        except Exception as e:
            logger.exception("fetch failed year=%d: %s", year, e)
            failed_years.append(year)
            continue
        if raw_df.empty:
            continue
        try:
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
    # indicator_id 単位でファイル分割
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

    # 失敗年があった場合も部分成功として 0 を返す（nightly が止まらないように）
    # ただし何も取れなかった場合は上で 1 を返している。
    return 0


if __name__ == "__main__":
    sys.exit(main())
