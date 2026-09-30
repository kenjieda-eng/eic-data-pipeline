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
    - 採用版 = 2021/2022/2023/2024/2025（base year 2019/2020/2021/2022/2023）。
      除外 = 2019（base year LCOE が Moderate でなく Constant/Low/Mid のみ）/
             2020（techdetail が LTRG・地名で Class 系非互換・default フラグ無し）。
      2024 は v4.0.0（2026-07-28 再配信）を採用。v3.0.0 と当方 31 系列の base year 値は全て同一（2026-09-29 実査）。
    - 抽出固定: scenario=Moderate / core_metric_case=Market。LCOE は財務指標のため crpyears=20。
      ★ 2025 年版は core_metric_case が Exp / Exp + TC / R&D / R&D + TC に改称された。一次
      （atb.nlr.gov「Financial Cases & Methods | Electricity | 2025 | ATB | NLR」、2026-09-29 実機確認）で
      「R&D + TC = 2024 年版の Markets and Policies（CSV では "Market"）に相当」と明記 → source_map の
      filter_case_by_edition で年版ごとに上書きする（2025: "R&D + TC"）。Exp 系は新設の別ケース（不採用）。
      CAPEX/CF は CRP 非依存（全 CRP で同値）のため crpyears 非フィルタ。
    - techdetail: ATB default フラグ（=1、2021/2022 は "1.0"・2023/2024 は "1" で正規化）で代表区分を選定。
      蓄電池のみ明示 techdetail="4Hr Battery Storage"（default フラグが 2023 で欠落するため）。
      ★ 2025 年版で ATB の既定区分が水力 NPD1 → NPD5、地熱 HydroFlash → HydroBinary に変わった（他 8 技術は
      不変）。既定に追随すると水力 CAPEX が 3,241 → 9,829 $/kW と区分違いで跳ぶため、水力・地熱は
      2021-2024 年版の既定区分（NPD1 / HydroFlash）を source_map で明示固定し、系列の連続性を保つ（2026-09-29）。
    - 原子力（Nuclear）は 2024 年版以降 2030 年以降の射影行のみで base year 行が無い → 2023 年版で終端
      （source_map の retired 宣言、2026-09-29）。
    - 新しい年版・版の検知: OEDI S3 の listing（?list-type=2&prefix=ATB/electricity/csv/）を走査し、editions に
      無い年版／採用版より新しい版があれば WARN（report-only。case 名の確認が要るため自動採用はしない）。
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
import re
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


LISTING_KEY_RE = re.compile(r"<Key>([^<]*?/(\d{4})/(?:(v[\d.]+)/)?ATBe\.csv)</Key>")
VERSION_RE = re.compile(r"v(\d+(?:\.\d+)*)")


def _version_key(text: str) -> tuple[int, ...]:
    """'v4.0.0' / '2024/v4.0.0/ATBe.csv' → (4, 0, 0)。版ディレクトリが無ければ (0,)。"""
    m = VERSION_RE.search(text)
    return tuple(int(p) for p in m.group(1).split(".")) if m else (0,)


def discover_new_editions(s3_base: str, editions: dict, log_dir: Path) -> list[str]:
    """
    OEDI S3 の listing を走査し、editions に無い年版（2021 以降）／採用版より新しい版を WARN で報告する。
    report-only（case 名の確認が要るため自動採用はしない）。listing が取れなければ何もしない。
    """
    try:
        scheme, _, host, prefix = s3_base.split("/", 3)
        url = f"{scheme}//{host}/?list-type=2&prefix={prefix.strip('/')}/"
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
        if r.status_code != 200:
            logger.warning("edition discovery: listing HTTP %s — skipped", r.status_code)
            return []
        found: dict[str, list[str]] = {}
        for _key, year, ver in LISTING_KEY_RE.findall(r.text):
            found.setdefault(year, []).append(ver or "(root)")
    except Exception as e:  # noqa: BLE001
        logger.warning("edition discovery: listing failed (%s) — skipped", e)
        return []
    news: list[str] = []
    for year, vers in sorted(found.items()):
        if int(year) < 2021:
            continue
        if year not in editions:
            news.append(f"{year}: {sorted(set(vers))} (not adopted)")
            continue
        adopted = str(editions[year])
        newer = sorted({v for v in vers if _version_key(v) > _version_key(adopted)})
        if newer:
            news.append(f"{year}: newer {newer} (adopted {adopted})")
    if news:
        msg = "new ATB editions/versions on OEDI (not adopted; check core_metric_case names first): " + "; ".join(news)
        logger.warning("edition discovery: %s", msg)
        append_log(log_dir, "fetch_nrel_atb", "WARN", msg)
    else:
        logger.info("edition discovery: OEDI listing has no newer edition/version than source_map (%d years seen)", len(found))
    return news


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
    # 年版ごとの core_metric_case 上書き（2025: "R&D + TC" = 2024 年版の Market に相当。docstring 参照）
    case_by_edition: dict = {str(k): str(v) for k, v in (src.get("filter_case_by_edition") or {}).items()}
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

    # 新しい年版・版の検知（report-only）
    discover_new_editions(s3_base, editions, log_dir)

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
        case_e = case_by_edition.get(str(edition), case)
        logger.info("edition %s: base_year=%d (将来年は不採用) case=%s", edition, base_year, case_e)

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
                case=case_e,
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
