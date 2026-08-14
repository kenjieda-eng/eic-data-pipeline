"""
EIA International Energy Statistics から国際 CO2 排出量 9 系列を取得するスクリプト。

対象（source_map.yaml の `eia-intl` セクション、年次）:
    地域別合計 6 系列（productId 4008 = CO2 emissions）
        - eia-co2-world  : WORL（世界）
        - eia-co2-jp     : JPN（日本）
        - eia-co2-cn     : CHN（中国）
        - eia-co2-us     : USA（米国）
        - eia-co2-eu27   : EU27
        - eia-co2-oecd   : OECD
    日本の燃料別内訳 3 系列（countryRegionId = JPN）
        - eia-co2-jp-coal : productId 4002（Coal and coke）
        - eia-co2-jp-oil  : productId 4006（Petroleum and other liquids）
        - eia-co2-jp-gas  : productId 4010（Consumed natural gas）

方式:
    GET https://api.eia.gov/v2/international/data/
        ?frequency=annual&data[0]=value
        &facets[activityId][]=8            (Emissions)
        &facets[productId][]=...
        &facets[countryRegionId][]=...
        &facets[unit][]=MMTCD              (million metric tonnes carbon dioxide)
    呼び出しは 2 回のみ:
        ① 6 地域 × productId 4008
        ② JPN × productId (4002, 4006, 4010)
    length=5000 上限 + offset ページング（response.total 超過時のみ継続）。

★★ facet 一覧エンドポイントは使わない ★★
    2026-08-14 実測で /v2/international/facet/{id} が返す値は石油系サブセットのみであり、
    実データに存在する値を取りこぼすことが判明している:
        activityId : 一覧 4 件（1,2,3,5）      ← 実データには 14 件（8=Emissions を含む）
        unit       : 一覧 3 件（MBBL,MT,TBPD） ← 実データには 17 件（MMTCD を含む）
        productId  : 一覧 18 件（石油＋輸入元別のみ） ← 実データには石炭/ガス/電力/CO2 が存在
    ルート説明文が "petroleum, natural gas, electricity, etc." と書いているのに一覧は石油だけ、
    という矛盾から発覚した。facet 一覧を信じて実装すると **CO2 の存在自体に気づけない**。
    → activityId / productId / countryRegionId / unit はすべて source_map.yaml の
      ハードコード値を明示指定する（一覧から動的に導出しない）。

★★ WP15 / WP17 / WP27 重複コードの罠 ★★
    countryRegionId には WP17（name="Japan", type="r"/Region）が別に存在し、
    JPN（type="c"/Country）と完全同値（2024 = 941.0）。WP15=China / WP27=United States も同様。
    countryRegionId をピン留めせず走査・集計すると日本が二重計上される。
    → check_gates() で「要求した countryRegionId と完全一致する行以外は弾く」ハード検証を行う。

ハード検証ゲート（1 本でも破れたら CSV を 1 行も書かずに exit 1）:
    a. 全行 unit == MMTCD、countryRegionId が要求値と完全一致
    b. 全共通年で |eia-co2-jp − (coal + oil + gas)| <= 0.05（加算整合）
    c. 行数下限 jp/cn/eu27 >= 40、us/world/oecd >= 70、全値 > 0

認証:
    環境変数 EIA_API_KEY（python-dotenv 経由で .env からも読む）。
    ★ キーの値はログにも source_url にも一切出さない（_redact() で URL からも除去）。
    未設定時は WARN ログを残して return 1。nightly-fetch.yml 側は continue-on-error: true
    なので他系列の取得は止まらない（fetch_estat.py と同じ規約）。

出力:
    - data/raw/eia_intl/eia_intl_{slug}_{YYYYMMDD}.json  （生レス）
    - data/processed/international/{indicator_id}.csv    （共通スキーマ long 形式）
    - data/processed/international/{indicator_id}.parquet
    - data/processed/international/{indicator_id}.metadata.json （D-011）

参考:
    - https://www.eia.gov/opendata/documentation.php
    - ライセンス: 米国連邦政府著作物、public domain（17 U.S.C. § 105）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from scripts.common.http import get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

# .env 読み込み（GitHub Actions では env: で渡される。ローカル開発時のみ有効）
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=ROOT / ".env")
except ImportError:
    pass  # python-dotenv 未インストール時は env 直読みのみ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_eia_intl")

SOURCE_KEY = "eia-intl"

PAGE_SIZE = 5000

# 加算整合ゲートの許容差（Mt-CO2）。実測は全年で 0.00 だが浮動小数の丸めに余裕を持たせる。
SUM_TOLERANCE = 0.05

# 行数下限（2026-08-14 実測: JPN/CHN/EU27 = 45 点、USA/WORL/OECD = 76 点）
MIN_ROWS = {
    "eia-co2-jp": 40,
    "eia-co2-cn": 40,
    "eia-co2-eu27": 40,
    "eia-co2-jp-coal": 40,
    "eia-co2-jp-oil": 40,
    "eia-co2-jp-gas": 40,
    "eia-co2-us": 70,
    "eia-co2-world": 70,
    "eia-co2-oecd": 70,
}

# 日本の燃料別内訳（加算整合ゲート b で eia-co2-jp と突き合わせる）
JP_BREAKDOWN = ("eia-co2-jp-coal", "eia-co2-jp-oil", "eia-co2-jp-gas")
JP_TOTAL = "eia-co2-jp"


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_api_key() -> str:
    """環境変数 EIA_API_KEY を読む。プレースホルダや空は無効扱い。"""
    key = (os.environ.get("EIA_API_KEY") or "").strip()
    if not key or key.lower() in {"your_api_key_here", "changeme", "todo"}:
        return ""
    return key


def _redact(text: str, key: str) -> str:
    """ログ出力用にキーの値を伏せる。★ ログ・例外文言は必ずこれを通す。"""
    if key and key in text:
        text = text.replace(key, "<REDACTED>")
    return text


def fetch_page(
    api_base: str,
    api_key: str,
    *,
    activity_id: str,
    product_ids: list[str],
    country_region_ids: list[str],
    unit_filter: str,
    offset: int,
) -> dict:
    """/v2/international/data/ を 1 ページ分叩いて response dict を返す。"""
    params: list[tuple[str, str]] = [
        ("frequency", "annual"),
        ("data[0]", "value"),
        ("facets[activityId][]", activity_id),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("offset", str(offset)),
        ("length", str(PAGE_SIZE)),
        ("facets[unit][]", unit_filter),
    ]
    for pid in product_ids:
        params.append(("facets[productId][]", pid))
    for cid in country_region_ids:
        params.append(("facets[countryRegionId][]", cid))
    # api_key は最後に足す（ログには出さない）
    params.append(("api_key", api_key))

    logger.info(
        "GET %s activityId=%s productId=%s countryRegionId=%s unit=%s offset=%d",
        api_base, activity_id, ",".join(product_ids),
        ",".join(country_region_ids), unit_filter, offset,
    )
    r = get(api_base, params=params, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(
            _redact(f"HTTP {r.status_code}: {r.text[:400]}", api_key)
        )
    payload = r.json() or {}
    resp = payload.get("response") or {}
    if "data" not in resp:
        raise RuntimeError(
            _redact(f"unexpected payload (no response.data): {r.text[:400]}", api_key)
        )
    return resp


def fetch_all(
    api_base: str,
    api_key: str,
    *,
    activity_id: str,
    product_ids: list[str],
    country_region_ids: list[str],
    unit_filter: str,
    raw_dir: Path,
    today_tag: str,
    slug: str,
) -> list[dict]:
    """total 超過分を offset ページングで取り切る。生レスは 1 ページ目を保存。"""
    rows: list[dict] = []
    offset = 0
    total = None
    page = 0
    while True:
        resp = fetch_page(
            api_base, api_key,
            activity_id=activity_id,
            product_ids=product_ids,
            country_region_ids=country_region_ids,
            unit_filter=unit_filter,
            offset=offset,
        )
        page += 1
        data = resp.get("data") or []
        if total is None:
            total = int(resp.get("total") or 0)
            # 生レスの保存は 1 ページ目のみ（api_key を含まない response 部分だけを残す）
            save_raw(
                json.dumps(resp, ensure_ascii=False, indent=2).encode("utf-8"),
                raw_dir,
                f"eia_intl_{slug}_{today_tag}.json",
            )
        rows.extend(data)
        logger.info("  page %d: +%d rows (total so far %d / %d)", page, len(data), len(rows), total)
        if not data or len(rows) >= total:
            break
        offset = len(rows)
    return rows


def rows_to_series(
    rows: list[dict],
    indicators: dict,
    wanted_ids: list[str],
) -> tuple[dict[str, list[tuple[str, float]]], list[str]]:
    """
    API 行を indicator_id ごとの [(period, value)] に振り分ける。

    振り分けキーは (countryRegionId, productId) の**完全一致**。
    WP17 等の重複コードや想定外の productId はここで拾われず、
    未知行として第 2 戻り値に記録される（ゲート a が exit 1 にする）。
    """
    lookup: dict[tuple[str, str], str] = {}
    for ind_id in wanted_ids:
        cfg = indicators[ind_id]
        lookup[(str(cfg["country_region_id"]), str(cfg["product_id"]))] = ind_id

    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    unknown: list[str] = []
    for row in rows:
        cid = str(row.get("countryRegionId") or "")
        pid = str(row.get("productId") or "")
        key = (cid, pid)
        ind_id = lookup.get(key)
        if ind_id is None:
            unknown.append(
                f"countryRegionId={cid} productId={pid} "
                f"({row.get('countryRegionName')} / {row.get('productName')}) "
                f"type={row.get('countryRegionTypeId')}"
            )
            continue
        v = row.get("value")
        if v is None or v == "":
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        period = str(row.get("period") or "").strip()
        if not period:
            continue
        out[ind_id].append((period, val))
    return out, unknown


def check_gates(
    rows: list[dict],
    series: dict[str, list[tuple[str, float]]],
    unknown: list[str],
    unit_filter: str,
    requested_country_ids: set[str],
) -> list[str]:
    """
    ハード検証ゲート a / b / c。破れた項目の説明文リストを返す（空なら合格）。
    """
    problems: list[str] = []

    # --- ゲート a: unit / countryRegionId の完全一致 -----------------------
    bad_unit = sorted({
        str(r.get("unit")) for r in rows if str(r.get("unit")) != unit_filter
    })
    if bad_unit:
        problems.append(f"[a] unit != {unit_filter} の行が存在: {bad_unit}")

    seen_ids = {str(r.get("countryRegionId") or "") for r in rows}
    extra_ids = sorted(seen_ids - requested_country_ids)
    if extra_ids:
        problems.append(
            f"[a] 要求外の countryRegionId が混入（WP15/WP17/WP27 型の重複コードの疑い）: {extra_ids}"
        )
    if unknown:
        sample = "; ".join(sorted(set(unknown))[:5])
        problems.append(f"[a] 振り分け不能な行 {len(unknown)} 件: {sample}")

    # --- ゲート c: 行数下限と正値 -----------------------------------------
    for ind_id, floor in MIN_ROWS.items():
        n = len(series.get(ind_id, []))
        if n < floor:
            problems.append(f"[c] {ind_id}: 行数 {n} < 下限 {floor}")
    for ind_id, pts in series.items():
        nonpos = [(p, v) for p, v in pts if not (v > 0)]
        if nonpos:
            problems.append(f"[c] {ind_id}: 非正値 {len(nonpos)} 件 (例 {nonpos[:3]})")

    # --- ゲート b: 加算整合 |jp - (coal+oil+gas)| <= 0.05 ------------------
    total_map = dict(series.get(JP_TOTAL, []))
    part_maps = [dict(series.get(k, [])) for k in JP_BREAKDOWN]
    common = set(total_map)
    for m in part_maps:
        common &= set(m)
    if not common:
        problems.append("[b] eia-co2-jp と内訳 3 系列の共通年が 0（突合不能）")
    else:
        worst_period, worst_diff = None, 0.0
        violations = []
        for period in sorted(common):
            diff = abs(total_map[period] - sum(m[period] for m in part_maps))
            if diff > abs(worst_diff):
                worst_period, worst_diff = period, diff
            if diff > SUM_TOLERANCE:
                violations.append((period, round(diff, 6)))
        if violations:
            problems.append(
                f"[b] 加算整合の破れ {len(violations)} 年 (許容 {SUM_TOLERANCE}): {violations[:5]}"
            )
        else:
            logger.info(
                "[b] 加算整合 OK: 共通 %d 年、最大差分 %.6f Mt (%s)",
                len(common), worst_diff, worst_period,
            )

    return problems


def build_df(ind_id: str, points: list[tuple[str, float]], region: str, source_url: str) -> pd.DataFrame:
    """
    共通スキーマ long 形式へ。
    観測日は**暦年の 1/1**（period "2024" → 2024-01-01）。
    既存の暦年年次系列（nrel-atb / eu-ets / estat-trade）と同じ規約。
    GIO の年度末規約（FY2024 → 2025-03-31）とは別物なので混同しないこと。
    """
    recs = [
        {
            "date": f"{period}-01-01",
            "indicator_id": ind_id,
            "region": region,
            "value": value,
            "source_url": source_url,
        }
        for period, value in sorted(points)
    ]
    return pd.DataFrame(recs, columns=["date", "indicator_id", "region", "value", "source_url"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EIA International CO2 emissions fetcher")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="取得と検証だけ行い、CSV / metadata を書き出さない",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    source_cfg = cfg["sources"][SOURCE_KEY]
    indicators = source_cfg["indicators"]
    indicator_ids = list(source_cfg["indicator_ids"])

    raw_dir = ROOT / "data" / "raw" / "eia_intl"
    processed_dir = ROOT / "data" / "processed" / "international"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    api_key = _get_api_key()
    if not api_key:
        # fetch_estat.py と同じ規約: WARN ログを残して return 1。
        # nightly-fetch.yml は continue-on-error: true なので他系列は止まらない。
        logger.error(
            "EIA_API_KEY is not set. EIA API v2 requires free registration "
            "(https://www.eia.gov/opendata/register.php). Skipping fetch."
        )
        append_log(log_dir, "fetch_eia_intl", "WARN", "EIA_API_KEY missing — skipped")
        return 1

    api_base = source_cfg["api_base"]
    activity_id = str(source_cfg["activity_id"])
    unit_filter = str(source_cfg["unit_filter"])

    # 呼び出し ①: 6 地域 × productId 4008 / ②: JPN × (4002, 4006, 4010)
    total_ids = [i for i in indicator_ids if str(indicators[i]["product_id"]) == "4008"]
    breakdown_ids = [i for i in indicator_ids if i in JP_BREAKDOWN]

    call1_countries = [str(indicators[i]["country_region_id"]) for i in total_ids]
    call2_products = [str(indicators[i]["product_id"]) for i in breakdown_ids]
    jp_country = str(indicators[JP_TOTAL]["country_region_id"])

    try:
        rows1 = fetch_all(
            api_base, api_key,
            activity_id=activity_id,
            product_ids=["4008"],
            country_region_ids=call1_countries,
            unit_filter=unit_filter,
            raw_dir=raw_dir, today_tag=today_tag, slug="totals",
        )
        rows2 = fetch_all(
            api_base, api_key,
            activity_id=activity_id,
            product_ids=call2_products,
            country_region_ids=[jp_country],
            unit_filter=unit_filter,
            raw_dir=raw_dir, today_tag=today_tag, slug="jp_breakdown",
        )
    except Exception as e:
        msg = _redact(str(e), api_key)
        logger.error("fetch failed: %s", msg)
        append_log(log_dir, "fetch_eia_intl", "FAIL", f"fetch failed: {msg[:200]}")
        return 1

    rows = rows1 + rows2
    logger.info("fetched %d rows total (%d + %d)", len(rows), len(rows1), len(rows2))

    series, unknown = rows_to_series(rows, indicators, indicator_ids)
    requested_country_ids = {str(indicators[i]["country_region_id"]) for i in indicator_ids}

    problems = check_gates(rows, series, unknown, unit_filter, requested_country_ids)
    if problems:
        # ★ 1 本でも破れたら CSV を 1 行も書かずに落とす。
        for p in problems:
            logger.error("GATE FAILED %s", p)
        append_log(
            log_dir, "fetch_eia_intl", "FAIL",
            f"validation gates failed ({len(problems)}): {problems[0][:160]}",
        )
        return 1
    logger.info("all validation gates passed (a: facet 一致 / b: 加算整合 / c: 行数・正値)")

    if args.dry_run:
        for ind_id in indicator_ids:
            pts = sorted(series[ind_id])
            logger.info(
                "[dry-run] %-18s n=%-3d %s..%s latest=%.1f",
                ind_id, len(pts), pts[0][0], pts[-1][0], pts[-1][1],
            )
        append_log(log_dir, "fetch_eia_intl", "OK", f"dry-run series={len(indicator_ids)}")
        return 0

    source_url = source_cfg["source_url"]
    written: list[str] = []
    total_rows = 0
    for ind_id in indicator_ids:
        ind_cfg = indicators[ind_id]
        df = build_df(ind_id, series[ind_id], str(ind_cfg["region"]), source_url)
        write_processed(df, processed_dir, ind_id)
        write_metadata_for_indicator(processed_dir, source_cfg, ind_id, df)
        written.append(ind_id)
        total_rows += len(df)
        logger.info(
            "%-18s n=%-3d %s..%s latest=%.1f Mt-CO2",
            ind_id, len(df), df["date"].iloc[0], df["date"].iloc[-1], df["value"].iloc[-1],
        )

    summary = f"series={len(written)} rows={total_rows}"
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_eia_intl", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
