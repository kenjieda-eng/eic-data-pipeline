"""
米国 Treasury Daily Yield Curve Rates (CMT) の取得スクリプト。

方式:
    GET https://home.treasury.gov/resource-center/data-chart-center/
        interest-rates/daily-treasury-rates.csv/{YYYY}/all
        ?type=daily_treasury_yield_curve&field_tdr_date_value={YYYY}&page&_format=csv

    CSV 構造（UTF-8）:
        行 1: 列ヘッダ "Date,1 Mo,1.5 Month,2 Mo,3 Mo,4 Mo,6 Mo,1 Yr,2 Yr,3 Yr,5 Yr,7 Yr,10 Yr,20 Yr,30 Yr"
              （年代によって列構成は若干変わる: 1.5 Month / 4 Mo は近年のみ、20 Yr は 1986-1993 / 2004- 限定）
        行 2+: データ。Date は MM/DD/YYYY、値は既に % 単位。

対象:
    4 系列（2y / 5y / 10y / 30y、いずれも CMT 利回り、% 単位）。

出力:
    - data/raw/us-treasury/treasury_yields_{YYYY}_{YYYYMMDD}.csv  （年別生ファイル）
    - data/processed/finance/{indicator_id}.csv                   （共通スキーマ long 形式）
    - data/processed/finance/{indicator_id}.parquet
    - data/processed/finance/{indicator_id}.metadata.json         （D-011）

参考:
    https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve
    ライセンス: 米国連邦政府公表データ、public domain（17 U.S.C. § 105）。
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.http import get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402
from scripts.common.metadata import (  # noqa: E402
    write_metadata_for_expected_indicators,
    write_metadata_for_indicator,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_us_treasury")

SOURCE_KEY = "us-treasury"
BACKFILL_START_YEAR = 1990


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_url(source_cfg: dict, year: int) -> str:
    return source_cfg["url_template"].format(
        base_url=source_cfg["base_url"],
        year=year,
    )


def fetch_year(year: int, source_cfg: dict, raw_dir: Path, today_tag: str) -> pd.DataFrame:
    """1 年分の CSV を取得し DataFrame で返す（生ファイルも保存）。空なら空 DF。"""
    url = build_url(source_cfg, year)
    logger.info("GET %s", url)
    r = get(url, timeout=60)
    r.raise_for_status()

    save_raw(r.content, raw_dir, f"treasury_yields_{year}_{today_tag}.csv")

    text = r.content.decode(source_cfg.get("encoding", "utf-8"), errors="replace")
    # まれに当年の早期に空/エラーレスポンスが返る場合は空 DF
    if "Date" not in text[:200]:
        logger.warning("year=%d: no 'Date' header in response — likely empty", year)
        return pd.DataFrame()

    df = pd.read_csv(io.StringIO(text))
    if "Date" not in df.columns:
        logger.warning("year=%d: 'Date' column missing after parse — skip", year)
        return pd.DataFrame()

    date_format = source_cfg.get("date_format", "%m/%d/%Y")
    df["_iso_date"] = pd.to_datetime(df["Date"], format=date_format, errors="coerce")
    bad = df["_iso_date"].isna().sum()
    if bad:
        logger.warning("year=%d: dropped %d rows with unparseable Date", year, bad)
    df = df.dropna(subset=["_iso_date"]).reset_index(drop=True)
    logger.info("year=%d: %d rows", year, len(df))
    return df


def normalize_to_long(
    df: pd.DataFrame,
    indicator_id: str,
    column: str,
    source_url: str,
) -> pd.DataFrame:
    """wide → long（共通スキーマ: date, indicator_id, region, value, source_url）。"""
    empty = pd.DataFrame(columns=["date", "indicator_id", "region", "value", "source_url"])
    if df.empty or column not in df.columns:
        if not df.empty:
            logger.warning("column '%s' not in DataFrame — skip %s", column, indicator_id)
        return empty

    out = df[["_iso_date", column]].rename(columns={column: "value"}).copy()
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out.dropna(subset=["value"])
    if out.empty:
        return empty

    out["date"] = out["_iso_date"].dt.strftime("%Y-%m-%d")
    out["indicator_id"] = indicator_id
    out["region"] = "us"
    out["source_url"] = source_url
    out = out[["date", "indicator_id", "region", "value", "source_url"]]
    out = out.sort_values("date").reset_index(drop=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch US Treasury daily CMT yields (4 series: 2y/5y/10y/30y)"
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help=f"全期間（{BACKFILL_START_YEAR} 〜 当年）を取得",
    )
    parser.add_argument(
        "--year", type=int, default=None,
        help="単一年を取得（省略時は当年のみ）",
    )
    parser.add_argument(
        "--series", type=str, default=None,
        help="カンマ区切りで indicator_id を絞る（例: us-treasury-10y）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    indicators_cfg: dict = source_cfg.get("indicators") or {}
    if not indicators_cfg:
        logger.error("source_map.yaml の %s.indicators が空です", SOURCE_KEY)
        return 2

    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}
        indicators_cfg = {k: v for k, v in indicators_cfg.items() if k in wanted}
        if not indicators_cfg:
            logger.error("--series で指定された ID が 1 つも該当しません: %s", args.series)
            return 2

    raw_dir = ROOT / "data" / "raw" / "us-treasury"
    processed_dir = ROOT / "data" / "processed" / "finance"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")
    current_year = now_jst.year

    if args.backfill:
        years = list(range(BACKFILL_START_YEAR, current_year + 1))
    elif args.year:
        years = [args.year]
    else:
        years = [current_year]

    all_dfs: list[pd.DataFrame] = []
    for y in years:
        try:
            df_y = fetch_year(y, source_cfg, raw_dir, today_tag)
            if not df_y.empty:
                all_dfs.append(df_y)
        except Exception as e:
            logger.exception("year=%d failed: %s", y, e)
            append_log(log_dir, "fetch_us_treasury", "WARN", f"year={y} failed: {e}")

    if not all_dfs:
        logger.error("no data fetched across years=%s", years)
        append_log(log_dir, "fetch_us_treasury", "FAIL", f"no data for years={years}")
        return 1

    big = pd.concat(all_dfs, ignore_index=True)
    # 同一 Date が当年再取得などで重複する可能性 → 後勝ちで dedup
    big = big.drop_duplicates(subset=["_iso_date"], keep="last").sort_values("_iso_date").reset_index(drop=True)
    logger.info(
        "merged %d rows across %d years (range=%s..%s)",
        len(big), len(all_dfs),
        big["_iso_date"].min().strftime("%Y-%m-%d"),
        big["_iso_date"].max().strftime("%Y-%m-%d"),
    )

    source_url = source_cfg.get("source_url", "")
    written: list[str] = []
    total_rows = 0
    for indicator_id, ind_cfg in indicators_cfg.items():
        column = ind_cfg.get("column")
        if not column:
            logger.warning("%s: column が未定義 — skip", indicator_id)
            continue
        long_df = normalize_to_long(big, indicator_id, column, source_url)
        if long_df.empty:
            logger.warning("%s: 0 rows after normalize — skip", indicator_id)
            continue
        write_processed(long_df, processed_dir, basename=indicator_id)
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, long_df)
        written.append(indicator_id)
        total_rows += len(long_df)
        logger.info(
            "%s: %d rows (range=%s..%s)",
            indicator_id, len(long_df), long_df["date"].min(), long_df["date"].max(),
        )

    if not written:
        logger.error("no series produced any rows")
        append_log(log_dir, "fetch_us_treasury", "FAIL", "no series produced rows")
        return 1

    # D-020④: フェッチ成功範囲で行が来なかった indicator も metadata を書き直す
    # （updated_at = 生存信号）。Treasury は年単位で fetch するため、対象年が全て
    # 成功（かつ空でない）ときだけ refresh する。全系列は同じ merged から導出される。
    meta_refreshed: list[str] = []
    meta_skipped: list[str] = []
    if len(all_dfs) == len(years):
        expected_ids = set(indicators_cfg)
        meta_refreshed, meta_skipped = write_metadata_for_expected_indicators(
            processed_dir, source_cfg, sorted(expected_ids - set(written))
        )
    else:
        logger.warning(
            "metadata refresh skipped: 一部の年の取得に失敗または空 "
            "— 失敗範囲の updated_at は進めない（D-020 §2.4 軸2 の故障隠蔽を防ぐ）"
        )
    logger.info(
        "metadata refreshed for row-less indicators: %d (skipped=%d)",
        len(meta_refreshed), len(meta_skipped),
    )
    if meta_skipped:
        logger.warning(
            "metadata refresh skipped (no CSV / unreadable cutoff): %s",
            ", ".join(meta_skipped),
        )

    summary = (
        f"series={len(written)} rows={total_rows} years={len(all_dfs)} "
        f"metadata_refreshed={len(meta_refreshed)}"
    )
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_us_treasury", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
