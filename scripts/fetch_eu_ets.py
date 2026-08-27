"""
EU ETS（EU 排出量取引制度）検証排出量データ（EUTL / European Environment Agency）から
ESG ドメインを seed するスクリプト（北極星 12 ドメイン唯一の未 seed を解消）。

ソース（datahub.io ミラー、安定 r-link、ODC-PDDL-1.0）:
    Family A: eu-ets-sector-emissions.csv   (sector, year, emissions_mt)  — EU 集計・部門別・Mt 単位・重複排除済み
    Family B: eu-ets.csv                     (country_code, main_activity_code, main_activity_name,
                                              citl_information, year, value) — 国 × 部門 × metric の原票（約 8MB）

系列設計（2 ファミリ・密度版）:
    Family A — EU 全体 部門別 検証排出量
        - CSV に存在する全部門に 1 系列。ID = eu-ets-emissions-{slug}。
        - slug は source_map.yaml の sector_slug_map（activity code → slug）。未マップ部門は
          name から kebab slug を導出してログ出力（L-013: 部門数は実データで確定）。
        - long: date=YYYY-01-01, region="EU-ETS", value=emissions_mt, source_url。
        - aggregation=raw, unit="Mt-CO2e", domain=esg, frequency=annual。

    Family B — 加盟国別 合計検証排出量
        フィルタ:
            - citl_information == "2.1 EU-ETS Verified Emission"
            - year が ^\\d{4}$（trading-period 文字列を除外）
            - main_activity_code が "-99" で終わらない（rollup 二重計上を除外）
            - country_code が ISO2（^[A-Z]{2}$）かつ特殊コード（XI 等）を除外
        集計:
            - 国 × 年で value を合計 → ÷ 1e6 で Mt 化。
            - ID = eu-ets-emissions-country-{cc}（cc=ISO2 小文字）。
            - long: date=YYYY-01-01, region=ISO2 大文字, value(Mt), source_url。
            - aggregation=annual_sum, unit="Mt-CO2e", domain=esg, frequency=annual。

    Family C — EU 全体 供給側（無償割当・オークション売却、年次）
        eu-ets.csv の供給側 metric を EU 全体・年次で 2 系列追加（2026-06-08）。
        フィルタ:
            - citl_information == ALLOWANCE_METRICS のラベル（1.1 無償割当 / 1.3 オークション）
            - year が ^\\d{4}$、country_code が ISO2 非 XI
            - main_activity_code in (10 航空, 20-99 全固定設備 rollup)
              ※供給側は leaf 部門別に按分されず、特にオークションは 10/20-99 の 2 コードのみ。
                leaf 合計（-99 除外）だと航空分のみに化けるため rollup 必須（L-062 実データ確認）。
        集計:
            - 年で value を合計 → ÷ 1e6 で「百万 EUA」化（1 EUA = 1 tCO2e）。
            - ID = eu-ets-allowances-allocated / eu-ets-allowances-auctioned。
            - long: date=YYYY-01-01, region="EU-ETS", value(百万 EUA), source_url。
            - aggregation=annual_sum, unit="百万 EUA", domain=esg, frequency=annual。

    Family D — 加盟国別 無償割当（年次）
        Family B（国別検証排出量）と対になる参照系列。Family C（供給側）の citl フィルタを
        Family B の国別 groupby で分解（2026-06-09 追加）。
        フィルタ:
            - citl_information == "1.1 Freely allocated allowances"
            - year が ^\\d{4}$、country_code が ISO2 非 XI
            - main_activity_code in (10 航空, 20-99 全固定設備 rollup)
              ※供給側は leaf 部門別に按分されないため Family C と同じく rollup 必須（L-062）。
        集計:
            - 国 × 年で value を合計 → ÷ 1e6 で「百万 EUA」化（1 EUA = 1 tCO2e）。
            - ID = eu-ets-allowances-allocated-country-{cc}（cc=ISO2 小文字）。
            - long: date=YYYY-01-01, region=ISO2 大文字, value(百万 EUA), source_url。
            - aggregation=annual_sum, unit="百万 EUA", domain=esg, frequency=annual。
            - 全国合計 == Family C eu-ets-allowances-allocated（同手法・国分解のため完全一致）。

二重計上の検証（必須）:
    実行ログに「Family B 全国合計（最新共通年）」と「Family A 固定設備部門合計（同年, 航空除く）」
    を print し、概ね一致を確認（航空は別枠なので固定設備分で照合）。大乖離なら rollup 混入を疑う。

ライセンス:
    EEA 再利用ポリシー（出典明記で商用再利用可）+ datahub 追加加工は ODC-PDDL-1.0。
    license: eea-terms

出力:
    - data/raw/eu-ets/eu_ets_sector_{YYYYMMDD}.csv  (Family A 生 CSV)
    - data/raw/eu-ets/eu_ets_full_{YYYYMMDD}.csv     (Family B 生 CSV)
    - data/processed/esg/{indicator_id}.csv / .parquet / .metadata.json
"""

from __future__ import annotations

import argparse
import io
import logging
import re
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

# Windows コンソール（cp932）でも日本語の二重計上検証 print が化けないように。
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_eu_ets")

SOURCE_KEY = "eu-ets"

VERIFIED_EMISSION_LABEL = "2.1 EU-ETS Verified Emission"
YEAR_RE = re.compile(r"^\d{4}$")
ISO2_RE = re.compile(r"^[A-Z]{2}$")

# Family C: EU 全体 供給側（無償割当・オークション売却）。
#   citl_information の正確な文字列は実 CSV で確認（L-062、推測しない）。オークションは
#   末尾 "(EUAs and EUAAs)" 付きが正。indicator_id -> citl_information ラベル。
ALLOWANCE_METRICS: dict[str, str] = {
    "eu-ets-allowances-allocated": "1.1 Freely allocated allowances",
    "eu-ets-allowances-auctioned": "1.3 Allowances auctioned or sold (EUAs and EUAAs)",
}
# 供給側 metric は leaf 部門別に按分されない（特にオークションは 10 航空 と 20-99 全固定
# 設備 rollup の 2 コードのみ）。EU 全体合計 = 有効国の 10(航空) + 20-99(全固定設備 rollup)。
# emissions/無償割当では leaf 合計と完全年で一致することを実データで検証済み（手法整合）。
# leaf 合計（-99 除外）だとオークションが航空分のみに化ける（実測 1/100 以下）ため rollup 必須。
ALLOWANCE_ROLLUP_CODES = ("10", "20-99")


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_csv(csv_url: str, *, min_bytes: int) -> bytes:
    """CSV を 1 リクエストで取得。サイズが極端に小さい場合は失敗扱い。"""
    logger.info("GET %s", csv_url)
    r = get(csv_url, timeout=180)
    r.raise_for_status()
    if len(r.content) < min_bytes:
        raise RuntimeError(
            f"CSV is suspiciously small ({len(r.content)} bytes < {min_bytes}); "
            f"preview={r.content[:300]!r}"
        )
    logger.info("downloaded %d bytes from %s", len(r.content), csv_url)
    return r.content


# --- Family A: EU 全体 部門別 ------------------------------------------------


def parse_sector_csv(csv_bytes: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(csv_bytes), dtype=str)
    required = {"sector", "year", "emissions_mt"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"sector CSV missing columns: {missing}. Got: {list(df.columns)}")
    df["emissions_mt"] = pd.to_numeric(df["emissions_mt"], errors="coerce")
    return df


def kebab(text: str) -> str:
    """name から kebab slug を導出（未マップ部門のフォールバック）。"""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "unknown"


def slug_for_sector(sector_str: str, slug_map: dict) -> tuple[str, str, bool]:
    """
    sector 文字列（例 '20 Combustion of fuels'）から (code, slug, unmapped) を返す。
    先頭の数字を activity code として slug_map を引き、無ければ name から kebab 導出。
    """
    m = re.match(r"^\s*(\d+)", sector_str)
    code = m.group(1) if m else ""
    if code and code in slug_map:
        return code, str(slug_map[code]), False
    # 未マップ: 先頭コードを除いた name 部分から kebab
    name_part = re.sub(r"^\s*\d+\s*", "", sector_str).strip()
    return code, kebab(name_part or sector_str), True


def process_family_a(
    df: pd.DataFrame,
    source_cfg: dict,
    source_url: str,
    processed_dir: Path,
    wanted: set[str] | None,
) -> tuple[list[str], int, list[str]]:
    """Family A（部門別）を処理。戻り値: (written_ids, total_rows, unmapped_logs)。"""
    slug_map = source_cfg.get("sector_slug_map") or {}
    # YAML で int キーになった場合に備えて str 正規化
    slug_map = {str(k): v for k, v in slug_map.items()}

    written: list[str] = []
    total_rows = 0
    unmapped_logs: list[str] = []

    for sector in sorted(df["sector"].dropna().unique()):
        code, slug, unmapped = slug_for_sector(sector, slug_map)
        if unmapped:
            msg = f"unmapped sector {sector!r} (code={code!r}) -> derived slug {slug!r}"
            logger.warning("Family A: %s", msg)
            unmapped_logs.append(msg)

        indicator_id = f"eu-ets-emissions-{slug}"
        if wanted is not None and indicator_id not in wanted:
            continue

        sub = df[df["sector"] == sector].copy()
        sub = sub.dropna(subset=["emissions_mt"])
        sub = sub[sub["year"].str.match(YEAR_RE, na=False)]
        if sub.empty:
            logger.warning("Family A: %s produced 0 rows", indicator_id)
            continue

        long_df = pd.DataFrame({
            "date": sub["year"].astype(str) + "-01-01",
            "indicator_id": indicator_id,
            "region": "EU-ETS",
            "value": sub["emissions_mt"].values,
            "source_url": source_url,
        })
        long_df = long_df.drop_duplicates(subset=["date", "indicator_id", "region"], keep="last")
        long_df = long_df.sort_values("date").reset_index(drop=True)

        write_processed(long_df, processed_dir, basename=indicator_id)
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, long_df)
        written.append(indicator_id)
        total_rows += len(long_df)
        logger.info(
            "Family A: %s: %d rows (%s..%s) [code=%s]",
            indicator_id, len(long_df), long_df["date"].min(), long_df["date"].max(), code,
        )

    return written, total_rows, unmapped_logs


# --- Family B: 加盟国別 合計 -------------------------------------------------


def parse_full_csv(csv_bytes: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(csv_bytes), dtype=str)
    required = {"country_code", "main_activity_code", "citl_information", "year", "value"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"full CSV missing columns: {missing}. Got: {list(df.columns)}")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def filter_verified_leaf(df: pd.DataFrame, exclude_countries: set[str]) -> pd.DataFrame:
    """
    検証排出量の leaf 行のみ抽出:
        - citl_information == 検証排出量ラベル
        - year が 4 桁年
        - main_activity_code が "-99"（rollup）で終わらない
        - country_code が ISO2 かつ特殊コード除外
    """
    mask = (
        (df["citl_information"] == VERIFIED_EMISSION_LABEL)
        & df["year"].str.match(YEAR_RE, na=False)
        & ~df["main_activity_code"].fillna("").str.endswith("-99")
        & df["country_code"].fillna("").str.match(ISO2_RE)
        & ~df["country_code"].isin(exclude_countries)
    )
    out = df.loc[mask].copy()
    out = out.dropna(subset=["value"])
    return out


def process_family_b(
    ve: pd.DataFrame,
    source_cfg: dict,
    source_url: str,
    processed_dir: Path,
    wanted: set[str] | None,
) -> tuple[list[str], int, list[str]]:
    """Family B（国別合計）を処理。戻り値: (written_ids, total_rows, unnamed_logs)。"""
    country_names = source_cfg.get("country_names") or {}

    grp = (
        ve.groupby(["country_code", "year"], as_index=False)["value"].sum()
    )
    grp["value_mt"] = grp["value"] / 1e6

    written: list[str] = []
    total_rows = 0
    unnamed_logs: list[str] = []

    for cc in sorted(grp["country_code"].unique()):
        indicator_id = f"eu-ets-emissions-country-{cc.lower()}"
        if wanted is not None and indicator_id not in wanted:
            continue
        if cc not in country_names:
            msg = f"country_code {cc!r} has no Japanese name in source_map (name falls back to id)"
            logger.warning("Family B: %s", msg)
            unnamed_logs.append(msg)

        sub = grp[grp["country_code"] == cc].copy()
        long_df = pd.DataFrame({
            "date": sub["year"].astype(str) + "-01-01",
            "indicator_id": indicator_id,
            "region": cc,
            "value": sub["value_mt"].values,
            "source_url": source_url,
        })
        long_df = long_df.drop_duplicates(subset=["date", "indicator_id", "region"], keep="last")
        long_df = long_df.sort_values("date").reset_index(drop=True)

        write_processed(long_df, processed_dir, basename=indicator_id)
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, long_df)
        written.append(indicator_id)
        total_rows += len(long_df)
        logger.info(
            "Family B: %s: %d rows (%s..%s)",
            indicator_id, len(long_df), long_df["date"].min(), long_df["date"].max(),
        )

    return written, total_rows, unnamed_logs


# --- Family C: EU 全体 供給側（無償割当・オークション） ----------------------


def process_family_c(
    df_full: pd.DataFrame,
    source_cfg: dict,
    source_url: str,
    processed_dir: Path,
    wanted: set[str] | None,
    exclude_countries: set[str],
) -> tuple[list[str], int, list[tuple[str, str, float]]]:
    """
    供給側 metric（無償割当・オークション売却）を EU 全体・年次で書き出す。
    集計: 有効国（ISO2 非 XI）の rollup コード（10 航空 + 20-99 全固定設備）を年で合計 →
          ÷1e6 で「百万 EUA」化（1 EUA = 1 tCO2e）。region="EU-ETS"。
    戻り値: (written_ids, total_rows, latest_values[(id, year, value)])。
    """
    written: list[str] = []
    total_rows = 0
    latest: list[tuple[str, str, float]] = []

    for indicator_id, label in ALLOWANCE_METRICS.items():
        if wanted is not None and indicator_id not in wanted:
            continue

        mask = (
            (df_full["citl_information"] == label)
            & df_full["year"].str.match(YEAR_RE, na=False)
            & df_full["country_code"].fillna("").str.match(ISO2_RE)
            & ~df_full["country_code"].isin(exclude_countries)
            & df_full["main_activity_code"].isin(ALLOWANCE_ROLLUP_CODES)
        )
        sub = df_full.loc[mask].dropna(subset=["value"])
        if sub.empty:
            logger.warning("Family C: %s produced 0 rows (label=%r)", indicator_id, label)
            continue

        grp = sub.groupby("year", as_index=False)["value"].sum()
        grp["value_m"] = grp["value"] / 1e6

        long_df = pd.DataFrame({
            "date": grp["year"].astype(str) + "-01-01",
            "indicator_id": indicator_id,
            "region": "EU-ETS",
            "value": grp["value_m"].values,
            "source_url": source_url,
        })
        long_df = long_df.drop_duplicates(subset=["date", "indicator_id", "region"], keep="last")
        long_df = long_df.sort_values("date").reset_index(drop=True)

        write_processed(long_df, processed_dir, basename=indicator_id)
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, long_df)
        written.append(indicator_id)
        total_rows += len(long_df)

        last_row = long_df.iloc[-1]
        last_year = str(last_row["date"])[:4]
        latest.append((indicator_id, last_year, float(last_row["value"])))
        logger.info(
            "Family C: %s: %d rows (%s..%s) latest %s=%.3f 百万EUA [label=%r]",
            indicator_id, len(long_df), long_df["date"].min(), long_df["date"].max(),
            last_year, float(last_row["value"]), label,
        )

    return written, total_rows, latest


# --- Family D: 加盟国別 無償割当 --------------------------------------------


def process_family_d(
    df_full: pd.DataFrame,
    source_cfg: dict,
    source_url: str,
    processed_dir: Path,
    wanted: set[str] | None,
    exclude_countries: set[str],
) -> tuple[list[str], int, list[str]]:
    """
    無償割当（1.1 Freely allocated allowances）を国 × 年で書き出す（Family B と対の参照系列）。
    Family C（供給側）の citl フィルタ + Family B（国別 groupby）の合成:
        - citl_information == ALLOWANCE_METRICS["eu-ets-allowances-allocated"]
        - year が 4 桁年 / country_code が ISO2（非 XI）/ main_activity_code が rollup（10 航空 + 20-99 全固定設備）
        - 国 × 年で value を合計 → ÷1e6 で「百万 EUA」化（1 EUA = 1 tCO2e）。
    供給側は leaf 部門按分が無いため rollup 必須（Family C と同手法。L-062）。
    ID = eu-ets-allowances-allocated-country-{cc}（cc=ISO2 小文字）、region = cc。
    戻り値: (written_ids, total_rows, unnamed_logs)。
    """
    country_names = source_cfg.get("country_names") or {}
    label = ALLOWANCE_METRICS["eu-ets-allowances-allocated"]

    mask = (
        (df_full["citl_information"] == label)
        & df_full["year"].str.match(YEAR_RE, na=False)
        & df_full["country_code"].fillna("").str.match(ISO2_RE)
        & ~df_full["country_code"].isin(exclude_countries)
        & df_full["main_activity_code"].isin(ALLOWANCE_ROLLUP_CODES)
    )
    sub_all = df_full.loc[mask].dropna(subset=["value"])

    grp = sub_all.groupby(["country_code", "year"], as_index=False)["value"].sum()
    grp["value_m"] = grp["value"] / 1e6

    written: list[str] = []
    total_rows = 0
    unnamed_logs: list[str] = []
    data_ccs = set(grp["country_code"].unique())

    for cc in sorted(data_ccs):
        indicator_id = f"eu-ets-allowances-allocated-country-{cc.lower()}"
        if wanted is not None and indicator_id not in wanted:
            continue
        if cc not in country_names:
            msg = f"country_code {cc!r} has no Japanese name in source_map (name falls back to id)"
            logger.warning("Family D: %s", msg)
            unnamed_logs.append(msg)

        sub = grp[grp["country_code"] == cc].copy()
        if sub.empty:
            logger.warning("Family D: %s produced 0 rows; skipped", indicator_id)
            continue

        long_df = pd.DataFrame({
            "date": sub["year"].astype(str) + "-01-01",
            "indicator_id": indicator_id,
            "region": cc,
            "value": sub["value_m"].values,
            "source_url": source_url,
        })
        long_df = long_df.drop_duplicates(subset=["date", "indicator_id", "region"], keep="last")
        long_df = long_df.sort_values("date").reset_index(drop=True)

        write_processed(long_df, processed_dir, basename=indicator_id)
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, long_df)
        written.append(indicator_id)
        total_rows += len(long_df)
        logger.info(
            "Family D: %s: %d rows (%s..%s)",
            indicator_id, len(long_df), long_df["date"].min(), long_df["date"].max(),
        )

    # 0 行国（無償割当データなし）を可視化（L-013: 国集合は実データから確定）。
    for cc in sorted(set(country_names) - data_ccs):
        logger.warning(
            "Family D: country %s has no '1.1 Freely allocated allowances' rows; no series", cc
        )

    return written, total_rows, unnamed_logs


# --- 二重計上の検証 ----------------------------------------------------------


def double_count_check(df_sector: pd.DataFrame, ve: pd.DataFrame) -> None:
    """
    Family B 全国合計（航空除く / 全活動）と Family A 固定設備部門合計（航空除く）を
    最新共通年で照合し print する。航空（code 10）は別枠なので固定設備分で比較。
    rollup 混入があれば B が約 2 倍に跳ねるため検知できる。
    """
    df_sector = df_sector.copy()
    df_sector["code"] = df_sector["sector"].str.extract(r"^(\d+)")
    years_a = set(df_sector.loc[df_sector["year"].str.match(YEAR_RE, na=False), "year"])
    years_b = set(ve["year"])
    common = sorted(years_a & years_b)
    if not common:
        logger.warning("二重計上検証: Family A/B に共通年が無くスキップ")
        return
    latest = common[-1]

    b_all = ve.loc[ve["year"] == latest, "value"].sum() / 1e6
    b_excl_av = ve.loc[(ve["year"] == latest) & (ve["main_activity_code"] != "10"), "value"].sum() / 1e6
    n_country = ve.loc[ve["year"] == latest, "country_code"].nunique()

    a_year = df_sector[df_sector["year"] == latest]
    a_all = a_year["emissions_mt"].sum()
    a_stationary = a_year.loc[a_year["code"] != "10", "emissions_mt"].sum()

    ratio = (b_excl_av / a_stationary) if a_stationary else float("nan")

    print("=" * 72)
    print(f"[二重計上の検証] 最新共通年 = {latest}")
    print(f"  Family B 全国合計（全活動, {n_country} か国, 航空込み）  : {b_all:9.1f} Mt-CO2e")
    print(f"  Family B 全国合計（航空除く=固定設備合計）              : {b_excl_av:9.1f} Mt-CO2e")
    print(f"  Family A 8 部門合計（航空込み）                        : {a_all:9.1f} Mt-CO2e")
    print(f"  Family A 固定設備部門合計（航空除く）                  : {a_stationary:9.1f} Mt-CO2e")
    print(f"  比 = Family B(航空除く) / Family A(航空除く)           : {ratio:9.3f}")
    print(f"  判定: B は全固定設備活動を含み A は主要 7 部門のみのため B ≳ A が正常。")
    print(f"        比が ~2.0 付近なら rollup(-99) 混入を疑う。今回 = {ratio:.3f}")
    print("=" * 72)
    logger.info(
        "double-count check: latest=%s B_excl_av=%.1f A_stationary=%.1f ratio=%.3f",
        latest, b_excl_av, a_stationary, ratio,
    )


# --- メイン ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch EU ETS verified emissions (ESG seed)")
    parser.add_argument(
        "--series", type=str, default=None,
        help="カンマ区切りで indicator_id を絞る（任意）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    csv_url_sector = source_cfg["csv_url_sector"]
    csv_url_full = source_cfg["csv_url_full"]
    source_url = source_cfg.get("source_url", csv_url_sector)
    exclude_countries = set(source_cfg.get("country_exclude") or [])

    raw_dir = ROOT / "data" / "raw" / "eu-ets"
    processed_dir = ROOT / "data" / "processed" / "esg"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    wanted: set[str] | None = None
    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}

    # --- 取得 ---
    try:
        sector_bytes = fetch_csv(csv_url_sector, min_bytes=1_000)
        full_bytes = fetch_csv(csv_url_full, min_bytes=1_000_000)
    except Exception as e:
        logger.exception("download failed: %s", e)
        append_log(log_dir, "fetch_eu_ets", "FAIL", f"download failed: {e}")
        return 1

    save_raw(sector_bytes, raw_dir, f"eu_ets_sector_{today_tag}.csv")
    save_raw(full_bytes, raw_dir, f"eu_ets_full_{today_tag}.csv")

    # --- パース ---
    try:
        df_sector = parse_sector_csv(sector_bytes)
        df_full = parse_full_csv(full_bytes)
    except Exception as e:
        logger.exception("parse failed: %s", e)
        append_log(log_dir, "fetch_eu_ets", "FAIL", f"parse failed: {e}")
        return 1

    ve = filter_verified_leaf(df_full, exclude_countries)
    logger.info(
        "Family B verified-leaf rows: %d (countries=%d, codes=%d)",
        len(ve), ve["country_code"].nunique(), ve["main_activity_code"].nunique(),
    )

    # --- Family A / B / C / D 処理 ---
    a_ids, a_rows, unmapped = process_family_a(df_sector, source_cfg, source_url, processed_dir, wanted)
    b_ids, b_rows, unnamed = process_family_b(ve, source_cfg, source_url, processed_dir, wanted)
    c_ids, c_rows, c_latest = process_family_c(
        df_full, source_cfg, source_url, processed_dir, wanted, exclude_countries
    )
    d_ids, d_rows, d_unnamed = process_family_d(
        df_full, source_cfg, source_url, processed_dir, wanted, exclude_countries
    )

    written = a_ids + b_ids + c_ids + d_ids
    if not written:
        logger.error("no series produced any rows")
        append_log(log_dir, "fetch_eu_ets", "FAIL", "no series produced rows")
        return 1

    # --- 二重計上の検証（print） ---
    double_count_check(df_sector, ve)

    # D-020④: フェッチ成功範囲で行が来なかった indicator も metadata を書き直す
    # （updated_at = 生存信号）。EUTL は 2 CSV を一括取得し
    # Family A/B/C/D を同じデータから導出するため、ここに到達した時点で
    # indicators の全 id がフェッチ成功範囲（失敗時は上で return 1）。
    expected_ids = {
        iid for iid in (source_cfg.get("indicators") or {})
        if wanted is None or iid in wanted
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

    summary = (
        f"Family A={len(a_ids)} series ({a_rows} rows), "
        f"Family B={len(b_ids)} series ({b_rows} rows), "
        f"Family C={len(c_ids)} series ({c_rows} rows), "
        f"Family D={len(d_ids)} series ({d_rows} rows), "
        f"total={len(written)} series, "
        f"metadata_refreshed={len(meta_refreshed)}, "
        f"unmapped_sectors={len(unmapped)}, unnamed_countries={len(unnamed) + len(d_unnamed)}"
    )
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_eu_ets", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
