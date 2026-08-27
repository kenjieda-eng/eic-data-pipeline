"""
NREL Annual Technology Baseline (ATB) 電力版から技術（tech）ドメイン 31 系列を取得するスクリプト。

対象（10 技術 × {LCOE, CAPEX, CF} + 蓄電池 CAPEX = 31 系列、年次）:
    技術: 陸上風力 / 洋上風力 / 太陽光（ユーティリティ・商業・住宅）/ CSP / 地熱 / 水力 /
          バイオマス / 原子力（LCOE/CAPEX/CF）+ 蓄電池 4hr（CAPEX のみ）
    指標:
        - atb-lcoe-{tech}  : 均等化発電原価 LCOE ($/MWh)
        - atb-capex-{tech} : 設備投資費 CAPEX ($/kW)
        - atb-cf-{tech}    : 設備利用率 (%, ATB の分数値 ×100)

⚠️ 編集方針（リク監修、最重要）:
    EIC Data は未来予測値を公開しない。ATB は 2050 までの将来射影を含むが、各年版（edition）の
    base year（= core_metric_variable の最小値 = その版の当年コスト推計）の行のみを採用し、
    将来年（base year 超）の行は取り込まない。横軸（date）は年版公表年（2021-01-01 等）。
    → 「NREL が各年版で推計した現在コスト」の近年系列（2021-2024、約 4 点）。

方式:
    OEDI Data Lake（S3 公開、no-sign-request）から HTTPS 直 URL で年版別 CSV を取得（aws cli 不要）:
        {s3_base}/{rel_path}   例 .../csv/2024/v3.0.0/ATBe.csv
    各版は不変のため、ローカル raw のサイズが remote Content-Length と一致すれば再利用する。

実 CSV 検証（L-062, 2026-06-06、6 版を S3 から実取得）:
    - 共通列: atb_year / core_metric_parameter / core_metric_case / crpyears / technology /
              techdetail / scenario / core_metric_variable / value（2019/2020 は revision 等が増減）。
    - 採用版 = 2021/2022/2023/2024（base year 2019/2020/2021/2022）。
      除外 = 2019（base year LCOE が Moderate でなく Constant/Low/Mid のみ）/
             2020（techdetail が LTRG・地名で Class 系非互換・default フラグ無し）。
    - 抽出固定: scenario=Moderate / core_metric_case=Market。LCOE は財務指標のため crpyears=20。
      CAPEX/CF は CRP 非依存（全 CRP で同値）のため crpyears 非フィルタ。
    - techdetail: ATB default フラグ（=1、2021/2022 は "1.0"・2023/2024 は "1" で正規化）で代表区分を選定。
      蓄電池のみ明示 techdetail="4Hr Battery Storage"（default フラグが 2023 で欠落するため）。
    - CF は分数（0-1）。metrics[*].scale=100 で % 化。

ライセンス:
    CC BY 4.0（OEDI submission に明記、L-063 確定）。
    license_notice: "Source: NREL Annual Technology Baseline (ATB), Open Energy Data Initiative (OEDI), CC BY 4.0."

出力（D-017: processed_dir は domain=tech に連動）:
    - data/raw/nrel-atb/atbe_{edition}.csv               (年版別 生 CSV)
    - data/processed/tech/{indicator_id}.csv             (共通スキーマ long 形式)
    - data/processed/tech/{indicator_id}.parquet
    - data/processed/tech/{indicator_id}.metadata.json   (D-011)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.http import USER_AGENT, get  # noqa: E402
from scripts.common.io import append_log, write_processed  # noqa: E402
from scripts.common.metadata import (  # noqa: E402
    write_metadata_for_expected_indicators,
    write_metadata_for_indicator,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_nrel_atb")

SOURCE_KEY = "nrel-atb"

# 全採用版に共通して存在する列（2019/2020 の revision 等は使わない）。
NEEDED_COLS = [
    "core_metric_parameter", "core_metric_case", "crpyears",
    "technology", "techdetail", "scenario", "core_metric_variable", "value",
]


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _is_default(series: pd.Series) -> pd.Series:
    """ATB default フラグを正規化（"1"/"1.0"/1 → True）。"""
    return series.fillna("0").map(lambda x: str(x).strip() in {"1", "1.0"}).astype(bool)


def _remote_size(url: str) -> int | None:
    """HEAD で Content-Length を取得（取れなければ None）。"""
    try:
        r = requests.head(
            url, headers={"User-Agent": USER_AGENT}, timeout=30, allow_redirects=True
        )
        if r.status_code == 200:
            cl = r.headers.get("Content-Length")
            return int(cl) if cl and cl.isdigit() else None
    except requests.RequestException:
        return None
    return None


def fetch_edition_csv(url: str, dest: Path) -> Path:
    """年版 CSV を取得。各版は不変のため、サイズ一致のローカルファイルがあれば再利用。"""
    rsize = _remote_size(url)
    if dest.exists() and rsize is not None and dest.stat().st_size == rsize:
        logger.info("reuse cached %s (%d bytes)", dest, rsize)
        return dest
    logger.info("GET %s", url)
    r = get(url, timeout=300)
    r.raise_for_status()
    if len(r.content) < 1_000_000:
        raise RuntimeError(
            f"ATB CSV is suspiciously small ({len(r.content)} bytes); url={url}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    logger.info("downloaded %s (%d bytes)", dest, len(r.content))
    return dest


def parse_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, low_memory=False)
    missing = [c for c in NEEDED_COLS if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"ATB CSV {path.name} missing required columns: {missing}. Got: {list(df.columns)}"
        )
    return df


def edition_base_year(df: pd.DataFrame) -> int:
    """その年版の base year（= core_metric_variable の最小値 = 当年コスト推計）。"""
    cmv = pd.to_numeric(df["core_metric_variable"], errors="coerce")
    return int(cmv.min())


def extract_value(
    df: pd.DataFrame,
    edition: str,
    base_year: int,
    *,
    tech_name: str,
    param: str,
    techdetail: str,
    crpyears: str | None,
    scenario: str,
    case: str,
) -> float | None:
    """
    1 (版, 技術, 指標) の base year 値を 1 つ取り出す。
    techdetail=="default" は ATB default フラグで代表区分を選定、それ以外は明示名で一致。
    抽出後に複数の異なる値が残る場合は曖昧として None（取り込まない）。
    """
    cmv = pd.to_numeric(df["core_metric_variable"], errors="coerce")
    mask = (
        (df["technology"] == tech_name)
        & (df["scenario"] == scenario)
        & (df["core_metric_case"] == case)
        & (df["core_metric_parameter"] == param)
        & (cmv == base_year)
    )
    if crpyears is not None:
        mask = mask & (df["crpyears"].astype(str) == str(crpyears))
    sub = df[mask]
    if sub.empty:
        return None

    if techdetail == "default":
        if "default" not in df.columns:
            return None
        sub = sub[_is_default(sub["default"])]
    else:
        sub = sub[sub["techdetail"] == techdetail]
    if sub.empty:
        return None

    vals = pd.to_numeric(sub["value"], errors="coerce").dropna()
    if vals.empty:
        return None

    uniq = sorted({round(float(v), 6) for v in vals})
    if len(uniq) > 1:
        tds = sorted(sub["techdetail"].dropna().unique().tolist())
        logger.warning(
            "%s %s %s: ambiguous (%d distinct values %s, techdetails=%s) — skip",
            edition, tech_name, param, len(uniq), uniq[:5], tds,
        )
        return None
    return uniq[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch NREL ATB tech-cost series (31 series)")
    parser.add_argument(
        "--series", type=str, default=None,
        help="カンマ区切りで indicator_id を絞る",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="（ATB は年版 CSV を常に全期間返すため通常モードと同じ。互換用フラグ）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        src = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    s3_base = src["s3_base"].rstrip("/")
    editions: dict = src["editions"]
    scenario = src["filter_scenario"]
    case = src["filter_case"]
    metrics: dict = src["metrics"]
    indicators: dict = src["indicators"]
    region = src.get("region", "US")
    source_url = src.get("source_url", "https://atb.nrel.gov/")
    domain = src.get("domain", "tech")

    raw_dir = ROOT / "data" / "raw" / "nrel-atb"
    processed_dir = ROOT / "data" / "processed" / domain
    log_dir = ROOT / "data" / "_logs"

    wanted: set[str] | None = None
    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}

    # indicator_id -> [(date, value), ...]
    accum: dict[str, list[tuple[str, float]]] = {}
    base_years: dict[str, int] = {}

    for edition, rel_path in editions.items():
        url = f"{s3_base}/{rel_path}"
        dest = raw_dir / f"atbe_{edition}.csv"
        try:
            path = fetch_edition_csv(url, dest)
            df = parse_csv(path)
        except Exception as e:
            logger.exception("edition %s: fetch/parse failed: %s", edition, e)
            append_log(log_dir, "fetch_nrel_atb", "WARN", f"edition {edition} failed: {e}")
            continue

        base_year = edition_base_year(df)
        base_years[edition] = base_year
        date = f"{edition}-01-01"
        logger.info("edition %s: base_year=%d (将来年は不採用)", edition, base_year)

        n_edition = 0
        for iid, icfg in indicators.items():
            if wanted is not None and iid not in wanted:
                continue
            metric = iid.split("-")[1]  # atb-{metric}-{slug}
            mc = metrics.get(metric)
            if mc is None:
                logger.warning("%s: 未知の metric '%s' — skip", iid, metric)
                continue
            val = extract_value(
                df, edition, base_year,
                tech_name=icfg["atb_technology"],
                param=mc["param"],
                techdetail=icfg["atb_techdetail"],
                crpyears=mc.get("crpyears"),
                scenario=scenario,
                case=case,
            )
            if val is None:
                continue
            scaled = val * float(mc.get("scale", 1))
            accum.setdefault(iid, []).append((date, scaled))
            n_edition += 1
        logger.info("edition %s: %d series matched", edition, n_edition)

    if not accum:
        logger.error("no series produced any rows")
        append_log(log_dir, "fetch_nrel_atb", "FAIL", "no series produced rows")
        return 1

    written: list[str] = []
    total_rows = 0
    for iid in indicators:
        if wanted is not None and iid not in wanted:
            continue
        rows = accum.get(iid)
        if not rows:
            logger.warning("%s: no data across editions — skip", iid)
            continue
        long_df = pd.DataFrame(rows, columns=["date", "value"])
        long_df["indicator_id"] = iid
        long_df["region"] = region
        long_df["source_url"] = source_url
        long_df = long_df[["date", "indicator_id", "region", "value", "source_url"]]
        long_df = long_df.sort_values("date").reset_index(drop=True)

        write_processed(long_df, processed_dir, basename=iid)
        write_metadata_for_indicator(processed_dir, src, iid, long_df)
        written.append(iid)
        total_rows += len(long_df)
        logger.info(
            "%s: %d pts (range=%s..%s) values=%s",
            iid, len(long_df), long_df["date"].min(), long_df["date"].max(),
            [round(float(v), 2) for v in long_df["value"]],
        )

    # D-020④: フェッチ成功範囲で行が来なかった indicator も metadata を書き直す
    # （updated_at = 生存信号）。ATB は edition 単位で fetch/parse するため、
    # 全 edition が成功したときだけ refresh する（base_years は成功時のみ埋まる）。
    meta_refreshed: list[str] = []
    meta_skipped: list[str] = []
    if len(base_years) == len(editions):
        expected_ids = {iid for iid in indicators if wanted is None or iid in wanted}
        meta_refreshed, meta_skipped = write_metadata_for_expected_indicators(
            processed_dir, src, sorted(expected_ids - set(written))
        )
    else:
        logger.warning(
            "metadata refresh skipped: 一部 edition の fetch/parse が失敗 "
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
        f"series={len(written)} rows={total_rows} "
        f"metadata_refreshed={len(meta_refreshed)} "
        f"editions={','.join(f'{e}(by={base_years[e]})' for e in base_years)}"
    )
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_nrel_atb", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
