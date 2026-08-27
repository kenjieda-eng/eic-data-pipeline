"""
Ember Monthly Electricity Data (long format CSV) から国際 50 系列を取得するスクリプト。

対象 (5 ヶ国 × 10 指標 = 50 系列, 月次):
    国: 日本 (jp), 米国 (us), 中国 (cn), ドイツ (de), 英国 (gb)
    指標:
        - ember-demand-{cc}        : 電力需要合計 (TWh)
        - ember-generation-{cc}    : 発電量合計   (TWh)
        - ember-co2-intensity-{cc} : 電力部門 CO2 排出強度 (gCO2/kWh)
        - ember-share-{fuel}-{cc}  : 電源種別 share of generation (%)
            fuel ∈ {coal, gas, nuclear, solar, wind, hydro, bioenergy} (7 種)
            #67 CO2 強度の「なぜ＝電源構成」を見せる主要国 電源構成比較 Insight の素材。

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
        ('Electricity generation', 'Fuel', <FuelName>) Unit='%' → 電源種別 share% (Coal/Gas/Nuclear/Solar/Wind/Hydro/Bioenergy)
            ※ 同じ Variable で Unit='TWh' と Unit='%' が両方存在するため Unit による絞り込み必須。

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
from scripts.common.metadata import (  # noqa: E402
    write_metadata_for_expected_indicators,
    write_metadata_for_indicator,
)

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


def parse_ember_dates(date_series: pd.Series) -> pd.Series:
    """
    Ember の Date 列を datetime に変換する。

    Ember は 2026-06 頃に Date フォーマットを ISO ``YYYY-MM-DD`` から
    ``DD/MM/YYYY`` (例 ``01/04/2018`` = 2018-04-01) へ変更した。月次データは
    常に月初なので日は ``01``、スラッシュ形式では先頭が「日」。

    pandas のデフォルト (``dayfirst=False``) はスラッシュ形式を ``MM/DD/YYYY`` と
    誤読し、月を「日」に取り違える (``01/04/2018`` → ``2018-01-04``)。これが
    2026-06-05 nightly での日付破損 (各月値が ``YYYY-01-<月>`` に化けて重複) の原因。

    逆に ``dayfirst=True`` を ISO 形式にかけると ``YYYY-DD-MM`` と誤読されるため、
    一律 dayfirst は使えない。区切り文字でフォーマットを判定して変換する。
    """
    sample = date_series.dropna().astype(str)
    if not sample.empty and sample.iloc[0].count("/"):
        # スラッシュ形式 = DD/MM/YYYY (Ember 2026-06 以降)
        return pd.to_datetime(date_series, format="%d/%m/%Y", errors="coerce")
    # ISO 形式 = YYYY-MM-DD (従来) もしくは未知 → pandas の ISO 解釈に任せる
    return pd.to_datetime(date_series, errors="coerce")


def extract_series(
    df: pd.DataFrame,
    area_name: str,
    category: str,
    subcategory: str,
    variable: str,
    indicator_id: str,
    region: str,
    source_url: str,
    unit: str | None = None,
) -> pd.DataFrame:
    """
    1 国 × 1 指標を抽出 → 共通スキーマ (date,indicator_id,region,value,source_url)。

    unit を指定すると Unit 列でも絞り込む（Electricity generation / Fuel は
    同じ Variable に Unit='TWh' と Unit='%' の両方が存在するため、share% 系列で必須）。
    """
    mask = (
        (df["Area"] == area_name)
        & (df["Area type"] == "Country or economy")
        & (df["Category"] == category)
        & (df["Subcategory"] == subcategory)
        & (df["Variable"] == variable)
    )
    if unit is not None:
        mask = mask & (df["Unit"] == unit)
    sub = df.loc[mask].copy()
    if sub.empty:
        logger.warning(
            "%s: 0 rows matched (Area=%r, Cat=%r, Sub=%r, Var=%r)",
            indicator_id, area_name, category, subcategory, variable,
        )
        return pd.DataFrame(columns=["date", "indicator_id", "region", "value", "source_url"])

    sub = sub.dropna(subset=["Value"])
    sub["date_dt"] = parse_ember_dates(sub["Date"])
    sub = sub.dropna(subset=["date_dt"])
    # 月次データは必ず月初。日が 01 でない行が出たら日付フォーマットの想定外変化
    # (= 破損の兆候) なので警告して気づけるようにする。
    non_month_start = int((sub["date_dt"].dt.day != 1).sum())
    if non_month_start:
        logger.warning(
            "%s: %d rows have day != 01 after date parsing — Date フォーマット変化の疑い "
            "(sample raw=%r)",
            indicator_id, non_month_start,
            sub.loc[sub["date_dt"].dt.day != 1, "Date"].head(3).tolist(),
        )
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
                unit=ind.get("unit"),
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

    # D-020④: フェッチ成功範囲で行が来なかった indicator も metadata を書き直す
    # （updated_at = 生存信号）。Ember は 1 CSV に全 country×template が入るため、
    # ここに到達した時点で全組み合わせがフェッチ成功範囲（失敗時は上で return 1）。
    expected_ids = {
        ind["id_template"].format(cc=country["region"])
        for country in countries
        for ind in indicators_def
        if wanted is None or ind["id_template"].format(cc=country["region"]) in wanted
    }
    meta_refreshed, meta_skipped = write_metadata_for_expected_indicators(
        processed_dir, source_cfg, sorted(expected_ids - set(written))
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

    summary = f"series={len(written)} rows={total_rows} metadata_refreshed={len(meta_refreshed)}"
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_ember", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
