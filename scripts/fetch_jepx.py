"""
JEPX スポット価格の取得スクリプト。

方針:
- 一次ソース: https://www.jepx.jp/electricpower/market-data/spot/
  （HTML を取得して、そこに載っている spot_summary_YYYY.csv リンクを抽出）
- URL は _download.php?timestamp=... という動的 URL なので、固定値では持たない
- 抽出できた URL をログに出してから取得

出力:
- data/raw/jepx/spot_summary_YYYY.csv         （生ファイル）
- data/processed/jepx/jepx-spot-{region}.csv  （共通スキーマ long 形式）
- data/processed/jepx/jepx-spot-{region}.parquet

デフォルト動作:
    python scripts/fetch_jepx.py
    → listing_url から見つかった CSV のうち、直近 N 年分（デフォルト 2）

オプション:
    --years N         直近 N 年分を取得（デフォルト 2）
    --year YYYY       特定の 1 年だけ取得
    --all             listing_url にあるすべての年を取得
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import pandas as pd
import yaml

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
# JEPX の実際の列名に合わせるため、複数バリエーションを許容
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


def _dump_html_diagnostics(html: str, raw_dir: Path) -> None:
    """HTML の中身をログと raw ファイルに出す（デバッグ用）。"""
    # raw ファイルとしても保存（後で Actions の Artifact から落とせるように）
    diag_path = raw_dir / "_listing_debug.html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    diag_path.write_text(html, encoding="utf-8")
    logger.info("saved listing HTML to %s (%d bytes)", diag_path, len(html))

    logger.info("--- HTML DIAGNOSTICS ---")
    logger.info("total length: %d chars", len(html))

    # 1) すべての href 値を列挙（最大 30 件）
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    logger.info("found %d href attributes", len(hrefs))
    for i, h in enumerate(hrefs[:30]):
        logger.info("  href[%d]: %s", i, h)

    # 2) "csv" を含む部分文字列の周辺 80 文字を抜き出し（最大 15 件）
    csv_re = re.compile(r".{0,40}\.csv.{0,40}", re.IGNORECASE)
    csv_hits = csv_re.findall(html)
    logger.info("found %d occurrences of '.csv'", len(csv_hits))
    for i, hit in enumerate(csv_hits[:15]):
        logger.info("  csv[%d]: %s", i, hit.strip())

    # 3) "spot" や "summary" を含む部分
    for keyword in ("spot_summary", "_download", "summary"):
        hits = re.findall(r".{0,30}" + keyword + r".{0,60}", html, re.IGNORECASE)
        logger.info("keyword '%s': %d hits", keyword, len(hits))
        for i, hit in enumerate(hits[:10]):
            logger.info("  %s[%d]: %s", keyword, i, hit.strip())

    # 4) <option> タグ（年度選択ドロップダウンがあるかも）
    options = re.findall(r"<option[^>]*>[^<]*</option>", html, re.IGNORECASE)
    logger.info("found %d <option> tags", len(options))
    for i, opt in enumerate(options[:15]):
        logger.info("  option[%d]: %s", i, opt)

    logger.info("--- END DIAGNOSTICS ---")


def extract_csv_links(
    listing_url: str,
    filename_pattern: str,
    raw_dir: Path,
) -> dict[int, str]:
    """
    市場データページの HTML から CSV のダウンロードリンクを抽出する。

    Returns:
        {year: absolute_url} の dict
    """
    logger.info("fetching listing page: %s", listing_url)
    resp = get(listing_url)
    resp.raise_for_status()

    # JEPX の HTML は UTF-8 が基本。念のため複数試す。
    html: str | None = None
    for encoding in ("utf-8", "cp932"):
        try:
            html = resp.content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if html is None:
        html = resp.text  # 最終手段

    # 1 次パターン: 「href="...spot_summary_YYYY.csv..."」
    href_re = re.compile(
        r'href=["\']([^"\']*' + filename_pattern + r'[^"\']*)["\']',
        re.IGNORECASE,
    )
    # 2 次パターン: onclick や data-* などに埋め込まれているケースも拾う
    generic_re = re.compile(
        r'["\']([^"\']*' + filename_pattern + r'[^"\']*)["\']',
        re.IGNORECASE,
    )
    year_re = re.compile(r"spot_summary_(\d{4})\.csv", re.IGNORECASE)

    found: dict[int, str] = {}

    def _harvest(regex: re.Pattern) -> None:
        for m in regex.finditer(html):
            raw_href = m.group(1)
            ym = year_re.search(raw_href)
            if not ym:
                continue
            year = int(ym.group(1))
            found[year] = urljoin(listing_url, raw_href)

    _harvest(href_re)
    if not found:
        # href で見つからなければ広域検索
        _harvest(generic_re)

    if not found:
        logger.error("no CSV links matched on %s", listing_url)
        _dump_html_diagnostics(html, raw_dir)
    else:
        logger.info(
            "extracted %d CSV link(s): %s",
            len(found),
            ", ".join(f"{y}={u}" for y, u in sorted(found.items())),
        )
    return found


def fetch_csv(url: str, year: int, raw_dir: Path) -> pd.DataFrame:
    """指定 URL を取得して DataFrame で返す。raw も保存。"""
    logger.info("fetching year=%d: %s", year, url)
    resp = get(url)
    if resp.status_code == 404:
        logger.warning("year=%d not found (404)", year)
        return pd.DataFrame()
    resp.raise_for_status()

    save_raw(resp.content, raw_dir, f"spot_summary_{year}.csv")

    for encoding in ("cp932", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(io.BytesIO(resp.content), encoding=encoding)
            logger.info("parsed year=%d rows=%d encoding=%s", year, len(df), encoding)
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


def pick_years(args: argparse.Namespace, available: Iterable[int]) -> list[int]:
    available = sorted(set(available))
    if not available:
        return []
    if args.year:
        return [args.year] if args.year in available else []
    if args.all:
        return list(available)
    # 直近 N 年分
    return available[-args.years:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch JEPX spot prices")
    parser.add_argument("--years", type=int, default=2,
                        help="直近 N 年分を取得（デフォルト 2）")
    parser.add_argument("--year", type=int, default=None,
                        help="特定の 1 年だけ取得（--years より優先）")
    parser.add_argument("--all", action="store_true",
                        help="listing_url にあるすべての年を取得")
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    listing_url: str = source_cfg["listing_url"]
    filename_pattern: str = source_cfg.get("filename_pattern", r"spot_summary_\d{4}\.csv")

    raw_dir = ROOT / "data" / "raw" / "jepx"
    processed_dir = ROOT / "data" / "processed" / "jepx"
    log_dir = ROOT / "data" / "_logs"

    try:
        year_to_url = extract_csv_links(listing_url, filename_pattern, raw_dir)
    except Exception as e:
        logger.exception("failed to extract CSV links: %s", e)
        append_log(log_dir, "fetch_jepx", "FAIL", f"listing extraction error: {e}")
        return 1

    if not year_to_url:
        append_log(log_dir, "fetch_jepx", "FAIL", "no CSV links found in listing page")
        return 1

    target_years = pick_years(args, year_to_url.keys())
    if not target_years:
        msg = f"no target years after filtering. available={sorted(year_to_url)}"
        logger.error(msg)
        append_log(log_dir, "fetch_jepx", "FAIL", msg)
        return 1
    logger.info("target years: %s", target_years)

    all_rows: list[pd.DataFrame] = []
    fetched_years: list[int] = []
    failed_years: list[int] = []

    for year in target_years:
        url = year_to_url[year]
        try:
            raw_df = fetch_csv(url, year, raw_dir)
        except Exception as e:
            logger.exception("fetch failed year=%d: %s", year, e)
            failed_years.append(year)
            continue
        if raw_df.empty:
            failed_years.append(year)
            continue
        try:
            norm = normalize(raw_df, source_url=url)
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
