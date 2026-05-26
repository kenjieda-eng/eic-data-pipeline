"""
Ember Monthly Electricity Data (long format CSV) から国際 15 系列を取得するスクリプト。

対象 (5 ヶ国 × 3 指標 = 15 系列, 月次):
    国: 日本 (jp), 米国 (us), 中国 (cn), ドイツ (de), 英国 (gb)
    指標:
        - ember-demand-{cc}        : 電力需要合計 (TWh)
        - ember-generation-{cc}    : 発電量合計   (TWh)
        - ember-co2-intensity-{cc} : 電力部門 CO2 排出強度 (gCO2/kWh)

方式:
    GET https://files.ember-energy.org/public-downloads/monthly_full_release_long_format.csv
    (約 70 MB の long-format CSV。1999-01〜最新月の全国全変数を 1 リクエストで取得)

実 API 検証 (2026-05-26):
    - CSV 構造 18 列: Area / ISO 3 code / Date / Area type / Continent / Ember region / EU / OECD / G20 / G7 / ASEAN
                       / Category / Subcategory / Variable / Unit / Value / YoY absolute change / YoY % change
    - 国名: 米国は "United States of America" (NOT "United States")
    - 抽出条件:
        ('Electricity demand', 'Demand', 'Demand')             → TWh
        ('Electricity generation', 'Total', 'Total Generation')→ TWh
        ('Power sector emissions', 'CO2 intensity', 'CO2 intensity') → gCO2/kWh

ライセンス:
    CC BY 4.0 (Creative Commons Attribution 4.0 International)
    出典明示を条件に再配布・商用利用可。
    license_notice: "Source: Ember (https://ember-energy.org/), licensed under CC BY 4.0."

出力:
    - data/raw/ember/ember_monthly_{YYYYMMDD}.csv          (生 CSV、約 70 MB)
    - data/processed/international/{indicator_id}.csv      (共通スキーマ long 形式)
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
logger = logging.getLogger("fetch_ember")

SOURCE_KEY = "ember"


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_csv(csv_url: str) -> bytes:
    """Ember の long-format CSV を 1 発で取得。"""
    logger.info("GET %s", csv_url)
    r = get(csv_url, timeout=180)
    r.raise_for_status()
    if len(r.content) < 1_000_000:
        raise RuntimeError(
            f"Ember CSV is suspiciously small ({len(r.content)} bytes); "
            f"preview={r.content[:300]!r}"
        )
    logger.info("downloaded %d bytes", len(r.content))
    return r.content


def parse_ember_csv(csv_bytes: bytes) -> pd.DataFrame:
    """
    Ember long-format CSV をパース。重要列のみ str/数値で整える。
    """
    df = pd.read_csv(io.BytesIO(csv_bytes), dtype=str)
    required = {"Area", "Date", "Area type", "Category", "Subcategory", "Variable", "Unit", "Value"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(
            f"Ember CSV missing required columns: {missing}. Got: {list(df.columns)}"
        )
    df["Value"] = pd.to_numeric(df["Value"], errors="coerce")
    return df


def extract_series(
    df: pd.DataFrame,
    area_name: str,
    category: str,
    subcategory: str,
    variable: str,
    indicator_id: str,
    region: str,
    source_url: str,
) -> pd.DataFrame:
    """
    1 国 × 1 指標を抽出 → 共通スキーマ (date,indicator_id,region,value,source_url)。
    """
    mask = (
        (df["Area"] == area_name)
        & (df["Area type"] == "Country or economy")
        & (df["Category"] == category)
        & (df["Subcategory"] == subcategory)
        & (df["Variable"] == variable)
    )
    sub = df.loc[mask].copy()
    if sub.empty:
        logger.warning(
            "%s: 0 rows matched (Area=%r, Cat=%r, Sub=%r, Var=%r)",
            indicator_id, area_name, category, subcategory, variable,
        )
        return pd.DataFrame(columns=["date", "indicator_id", "region", "value", "source_url"])

    sub = sub.dropna(subset=["Value"])
    sub["date_dt"] = pd.to_datetime(sub["Date"], errors="coerce")
    sub = sub.dropna(subset=["date_dt"])
    sub["date"] = sub["date_dt"].dt.strftime("%Y-%m-%d")
    out = pd.DataFrame({
        "date": sub["date"].values,
        "indicator_id": indicator_id,
        "region": region,
        "value": sub["Value"].values,
        "source_url": source_url,
    })
    out = out.drop_duplicates(subset=["date", "indicator_id", "region"], keep="last")
    out = out.sort_values("date").reset_index(drop=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Ember monthly electricity (15 series)")
    parser.add_argument(
        "--backfill", action="store_true",
        help="（Ember CSV は常に全期間を返すため通常モードと同じ。互換用フラグ）",
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

    csv_url = source_cfg["csv_url"]
    countries: list[dict] = source_cfg["countries"]
    indicators_def: list[dict] = source_cfg["indicator_templates"]

    raw_dir = ROOT / "data" / "raw" / "ember"
    processed_dir = ROOT / "data" / "processed" / "international"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    # CSV 取得
    try:
        csv_bytes = fetch_csv(csv_url)
    except Exception as e:
        logger.exception("download failed: %s", e)
        append_log(log_dir, "fetch_ember", "FAIL", f"download failed: {e}")
        return 1

    save_raw(csv_bytes, raw_dir, f"ember_monthly_{today_tag}.csv")

    # パース
    try:
        df = parse_ember_csv(csv_bytes)
    except Exception as e:
        logger.exception("parse failed: %s", e)
        append_log(log_dir, "fetch_ember", "FAIL", f"parse failed: {e}")
        return 1

    source_url = source_cfg.get("source_url", csv_url)

    # --series 絞り込み
    wanted: set[str] | None = None
    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}

    written: list[str] = []
    total_rows = 0

    for country in countries:
        area_name = country["area_name"]
        region = country["region"]
        for ind in indicators_def:
            id_template = ind["id_template"]
            indicator_id = id_template.format(cc=region)
            if wanted is not None and indicator_id not in wanted:
                continue

            long_df = extract_series(
                df,
                area_name=area_name,
                category=ind["category"],
                subcategory=ind["subcategory"],
                variable=ind["variable"],
                indicator_id=indicator_id,
                region=region,
                source_url=source_url,
            )
            if long_df.empty:
                continue

            write_processed(long_df, processed_dir, basename=indicator_id)
            write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, long_df)
            written.append(indicator_id)
            total_rows += len(long_df)
            logger.info(
                "%s: %d rows (range=%s..%s)",
                indicator_id, len(long_df),
                long_df["date"].min(), long_df["date"].max(),
            )

    if not written:
        logger.error("no series produced any rows")
        append_log(log_dir, "fetch_ember", "FAIL", "no series produced rows")
        return 1

    summary = f"series={len(written)} rows={total_rows}"
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_ember", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
