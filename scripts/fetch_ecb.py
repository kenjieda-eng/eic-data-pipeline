"""
ECB SDMX API から国際指標 5 系列を取得するスクリプト。

対象 (5 系列, 月次):
    --- 政策金利 (dataflow FM, daily, 変化日のみ tick) → 月末値に集約 ---
    - ecb-rate-dfr  : Deposit Facility Rate
    - ecb-rate-mrr  : Main Refinancing Operations Rate (fixed rate tenders)
    - ecb-rate-mlf  : Marginal Lending Facility Rate

    --- 為替 (dataflow EXR, 月次レート) ---
    - fx-eurusd-monthly-avg : EUR/USD ECB reference rate (USD per EUR)
    - fx-eurjpy-monthly-avg : EUR/JPY ECB reference rate (JPY per EUR)

方式:
    GET https://data-api.ecb.europa.eu/service/data/{flowRef}/{key}
    Accept: text/csv
    params: startPeriod=YYYY-MM, endPeriod=YYYY-MM

実 API 検証 (2026-05-26):
    - FM/D.U2.EUR.4F.KR.{DFR|MRR_FR|MLFR}.LEV  → 200 OK (FREQ=D, "date of changes (raw data)")
    - EXR/M.{USD|JPY}.EUR.SP00.A               → 200 OK (FREQ=M)
    月次集約版 (FM/M.*.LEV) は 404。daily で取得し ffill + 月末値抽出で月次化する。

ライセンス:
    ECB のオープンデータ。出典明示を条件に再利用可。license_notice に
    "Source: European Central Bank. Reuse permitted with attribution." を明記。

出力:
    - data/raw/ecb/ecb_{flow}_{key_slug}_{YYYYMMDD}.csv  (生 CSV)
    - data/processed/international/{indicator_id}.csv    (共通スキーマ long 形式)
    - data/processed/international/{indicator_id}.parquet
    - data/processed/international/{indicator_id}.metadata.json
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
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_ecb")

SOURCE_KEY = "ecb"
BACKFILL_START = "1999-01"   # ECB 設立


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_series_csv(
    base_url: str,
    flow_ref: str,
    key: str,
    start_period: str,
    end_period: str,
) -> bytes:
    """ECB SDMX REST API で 1 系列の CSV を取得。"""
    url = f"{base_url.rstrip('/')}/{flow_ref}/{key}"
    params = {"startPeriod": start_period, "endPeriod": end_period}
    logger.info("GET %s params=%s", url, params)
    r = get(url, params=params, headers={"Accept": "text/csv"}, timeout=60)
    r.raise_for_status()
    if not r.content or len(r.content) < 200:
        raise RuntimeError(
            f"ECB CSV is suspiciously small ({len(r.content)} bytes); preview={r.content[:300]!r}"
        )
    return r.content


def parse_ecb_csv(csv_bytes: bytes) -> pd.DataFrame:
    """
    ECB SDMX-CSV (data messages format) をパースし、TIME_PERIOD と OBS_VALUE 列だけを返す。

    両 dataflow (FM/EXR) でカラム構成は異なるが、TIME_PERIOD と OBS_VALUE は共通。
    """
    df = pd.read_csv(io.BytesIO(csv_bytes), dtype=str)
    if "TIME_PERIOD" not in df.columns or "OBS_VALUE" not in df.columns:
        raise RuntimeError(
            f"ECB CSV missing TIME_PERIOD/OBS_VALUE columns. Got: {list(df.columns)[:15]}"
        )
    out = df[["TIME_PERIOD", "OBS_VALUE"]].copy()
    out["OBS_VALUE"] = pd.to_numeric(out["OBS_VALUE"], errors="coerce")
    out = out.dropna(subset=["OBS_VALUE"])
    return out


def to_monthly_long(
    df_obs: pd.DataFrame,
    indicator_id: str,
    region: str,
    source_url: str,
    *,
    daily_to_monthly: bool,
) -> pd.DataFrame:
    """
    パース済み TIME_PERIOD/OBS_VALUE を共通スキーマ (date,indicator_id,region,value,source_url) に正規化。

    daily_to_monthly=True の場合は日次→月末値抽出 (政策金利の change-date 系列を月次化)。
    """
    out = df_obs.rename(columns={"OBS_VALUE": "value"}).copy()
    out["date_dt"] = pd.to_datetime(out["TIME_PERIOD"], errors="coerce")
    out = out.dropna(subset=["date_dt"])

    if daily_to_monthly:
        # 日次に並べ替え → asfreq('D', ffill) で穴埋め → 月末値抽出
        s = out.set_index("date_dt")["value"].sort_index()
        # 同日複数 obs は最後（=最新の change date 値）を採用
        s = s[~s.index.duplicated(keep="last")]
        s_daily = s.asfreq("D", method="ffill")
        # 月末値: 各月の最終日の値
        s_month_end = s_daily.resample("ME").last().dropna()
        out = s_month_end.reset_index()
        out.columns = ["date_dt", "value"]

    # date は月初日に正規化（既存系列と整合: 例 2026-04 → 2026-04-01）
    out["date"] = out["date_dt"].dt.to_period("M").dt.to_timestamp().dt.strftime("%Y-%m-%d")
    out["indicator_id"] = indicator_id
    out["region"] = region
    out["source_url"] = source_url
    out = out[["date", "indicator_id", "region", "value", "source_url"]]
    out = out.drop_duplicates(subset=["date", "indicator_id", "region"], keep="last")
    out = out.sort_values("date").reset_index(drop=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch ECB rates & FX (5 series)")
    parser.add_argument(
        "--backfill", action="store_true",
        help=f"全期間 ({BACKFILL_START} 〜 現在) を取得",
    )
    parser.add_argument(
        "--series", type=str, default=None,
        help="カンマ区切りで indicator_id を絞る",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    base_url = source_cfg["base_url"]
    series_defs: list[dict] = source_cfg["series"]

    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}
        series_defs = [s for s in series_defs if s["id"] in wanted]
        if not series_defs:
            logger.error("--series で指定された ID が 1 つも該当しません: %s", args.series)
            return 2

    raw_dir = ROOT / "data" / "raw" / "ecb"
    processed_dir = ROOT / "data" / "processed" / "international"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")
    current_ym = now_jst.strftime("%Y-%m")

    if args.backfill:
        start_period = BACKFILL_START
    else:
        # 通常モード: 直近 3 年分（差分マージで CSV は historic 保持）
        start_period = (now_jst.replace(year=now_jst.year - 3)).strftime("%Y-%m")
    end_period = current_ym

    written: list[str] = []
    total_rows = 0

    for sd in series_defs:
        indicator_id = sd["id"]
        flow_ref = sd["flow_ref"]
        key = sd["key"]
        region = sd.get("region", "eu")
        daily_to_monthly = bool(sd.get("daily_to_monthly", False))

        try:
            csv_bytes = fetch_series_csv(base_url, flow_ref, key, start_period, end_period)
        except Exception as e:
            logger.exception("%s: download failed: %s", indicator_id, e)
            append_log(log_dir, "fetch_ecb", "WARN", f"{indicator_id}: download failed: {e}")
            continue

        # raw 保存
        key_slug = key.replace(".", "_")
        save_raw(csv_bytes, raw_dir, f"ecb_{flow_ref}_{key_slug}_{today_tag}.csv")

        try:
            df_obs = parse_ecb_csv(csv_bytes)
        except Exception as e:
            logger.exception("%s: parse failed: %s", indicator_id, e)
            append_log(log_dir, "fetch_ecb", "WARN", f"{indicator_id}: parse failed: {e}")
            continue

        source_url = f"{base_url.rstrip('/')}/{flow_ref}/{key}"
        long_df = to_monthly_long(
            df_obs, indicator_id, region, source_url,
            daily_to_monthly=daily_to_monthly,
        )
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
        append_log(log_dir, "fetch_ecb", "FAIL", "no series produced rows")
        return 1

    summary = f"series={len(written)} rows={total_rows}"
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_ecb", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
