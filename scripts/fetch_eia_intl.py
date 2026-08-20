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
    派生 12 系列（2026-08-20 追加、catalog 590 → 602）
        - eia-co2-per-capita-{world,jp,cn,us,eu27,oecd} : t-CO2/人
        - eia-co2-per-gdp-{world,jp,cn,us,eu27,oecd}    : t-CO2/百万2015年PPPドル
          分母は EIA の人口（activityId 33 / productId 4702 / THP）と
          実質 GDP（activityId 34 / productId 4701 / BDOLPPP）。
          ★ 分母自体は系列化しない（materialize しない）。生値は data/raw の生レスに残る。

方式:
    GET https://api.eia.gov/v2/international/data/
        ?frequency=annual&data[0]=value
        &facets[activityId][]=8            (Emissions)
        &facets[productId][]=...
        &facets[countryRegionId][]=...
        &facets[unit][]=MMTCD              (million metric tonnes carbon dioxide)
    呼び出しは 4 回のみ:
        ① 6 地域 × productId 4008
        ② JPN × productId (4002, 4006, 4010)
        ③ 6 地域 × activityId 33 / productId 4702 / unit THP      （人口・分母）
        ④ 6 地域 × activityId 34 / productId 4701 / unit BDOLPPP  （実質GDP・分母）
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
    ★ 2026-08-20 実測: この重複コードは人口・GDP にも存在する（WP17 の 2024 年値は
      人口 123,753 千人 / GDP 5,311.332 で JPN と完全同値）。分母側も同じ規律で弾く。

★★ activityId 33 / 34 には productId 47（Energy intensity）が同居する ★★
    activityId=33（Population）… productId 4702 = Population (THP)
                                  productId 47   = Energy intensity (MBTUPP = million Btu per person)
    activityId=34（GDP）       … productId 4701 = Gross domestic product (BDOLPPP)
                                  productId 47   = Energy intensity (TBTUUSDPP = thousand Btu per USD PPP)
    activityId だけで絞ると「エネルギー原単位」を人口・GDP として取り込んでしまう。
    → productId / unit を明示指定し、取得後も完全一致を検証する（分母ゲート a / c）。

ハード検証ゲート（1 本でも破れたら CSV を 1 行も書かずに exit 1）:
  CO2 側:
    a. 全行 unit == MMTCD、countryRegionId が要求値と完全一致
    b. 全共通年で |eia-co2-jp − (coal + oil + gas)| <= 0.05（加算整合）
    c. 行数下限 jp/cn/eu27 >= 40、us/world/oecd >= 70、全値 > 0
  派生（分母）側:
    a. productId 完全一致（4702 / 4701）★ 47 = Energy intensity の混入を弾く
    b. countryRegionId 完全一致（WP15 / WP17 / WP27 型の重複コードを弾く）
    c. unit 完全一致（THP / BDOLPPP）、コード別の行数下限
    d. 年の突合 — CO2 に存在しない period（分母だけが持つ 2025 年など）は出力しない
    e. レンジゲート — 一人当たり 0.5〜30 t/人、GDP 当たり 50〜3000 t/百万$
    f. 基準年カナリア — GDP の基準年が 2015 のままかを毎回検証。
       米国は PPP の numeraire（換算 = 1）なので、基準年では実質 = 名目が成立する。
       EIA の USA 2015 = 18,295.000 は BEA 米名目 GDP 2015 = 18,295.019 十億ドルと一致する。
       ★ 基準年は unit 文字列（BDOLPPP）に含まれないため、EIA がサイレントに
         リベースしてもゲート c では気づけない。このカナリアでしか検知できない。

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

# --- 派生 12 系列 --------------------------------------------------------
# ★★ 命名を intensity ではなく per-gdp にした理由 ★★
#   既存の ember-co2-intensity-*（5 系列, international）は「電力部門の排出強度」で
#   分子 = 発電由来 CO2・分母 = 発電量（gCO2/kWh）。本系列は「経済規模あたりの排出」で
#   分子 = 経済全体のエネルギー起源 CO2・分母 = 実質 GDP と、分子・分母とも別物である。
#   英語ではどちらも carbon intensity と呼ばれるため、系列 ID の時点で混同させないよう
#   intensity を避けて per-gdp を採用した（2026-08-20 リン判断）。
DERIVED_PER_CAPITA = "per_capita"
DERIVED_PER_GDP = "per_gdp"

# derived_kind → source_map の derived.denominators のキー
DERIVED_DENOM_KEY = {
    DERIVED_PER_CAPITA: "population",
    DERIVED_PER_GDP: "gdp",
}

# 単位換算スケール（分子 CO2 は Mt = 1e6 t 固定）:
#   per_capita: Mt ÷ 千人      = (v * 1e6 t) / (d * 1e3 人)   → t-CO2/人      … × 1e3
#   per_gdp   : Mt ÷ 十億ドル  = (v * 1e6 t) / (d * 1e3 百万$) → t-CO2/百万$   … × 1e3
DERIVED_SCALE = {
    DERIVED_PER_CAPITA: 1e3,
    DERIVED_PER_GDP: 1e3,
}

# 派生値の丸め桁（浮動小数のノイズを CSV に残さない）
DERIVED_ROUND = 6


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


def denominator_rows_to_map(
    rows: list[dict],
    requested_country_ids: set[str],
) -> dict[str, dict[str, float]]:
    """
    分母（人口 / GDP）の API 行を {countryRegionId: {period: value}} に畳む。

    要求外の countryRegionId（WP15 / WP17 / WP27 型の重複コード）はここで捨てるが、
    「捨てた」こと自体は check_denominator_gates() が exit 1 にする。
    黙って捨てると重複コードの混入に気づけないため、検知と破棄は必ず分けること。
    """
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        cid = str(row.get("countryRegionId") or "")
        if cid not in requested_country_ids:
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
        out[cid][period] = val
    return dict(out)


def check_denominator_gates(
    label: str,
    rows: list[dict],
    denom_cfg: dict,
    denom_map: dict[str, dict[str, float]],
    requested_country_ids: set[str],
    min_rows: int,
) -> list[str]:
    """
    分母側のハード検証ゲート a / b / c。破れた項目の説明文リストを返す（空なら合格）。

    a: productId 完全一致 — ★ activityId 33 / 34 には productId 47（Energy intensity）が
       同居するため、これが無いと「エネルギー原単位」を人口・GDP として取り込む。
    b: countryRegionId 完全一致 — WP15 / WP17 / WP27 型の重複コードを弾く。
    c: unit 完全一致 + コード別の行数下限 + 全値 > 0。
    """
    problems: list[str] = []
    want_pid = str(denom_cfg["product_id"])
    want_unit = str(denom_cfg["unit"])

    # --- ゲート a: productId 完全一致 -------------------------------------
    bad_pid = sorted({
        f"{r.get('productId')}({r.get('productName')})"
        for r in rows if str(r.get("productId")) != want_pid
    })
    if bad_pid:
        problems.append(
            f"[a] {label}: productId != {want_pid} の行が混入"
            f"（47 = Energy intensity の取り込みの疑い）: {bad_pid}"
        )

    # --- ゲート b: countryRegionId 完全一致 -------------------------------
    seen_ids = {str(r.get("countryRegionId") or "") for r in rows}
    extra_ids = sorted(seen_ids - requested_country_ids)
    if extra_ids:
        problems.append(
            f"[b] {label}: 要求外の countryRegionId が混入"
            f"（WP15 / WP17 / WP27 型の重複コードの疑い）: {extra_ids}"
        )

    # --- ゲート c: unit 完全一致 / 行数下限 / 正値 ------------------------
    bad_unit = sorted({
        str(r.get("unit")) for r in rows if str(r.get("unit")) != want_unit
    })
    if bad_unit:
        problems.append(f"[c] {label}: unit != {want_unit} の行が存在: {bad_unit}")

    for cid in sorted(requested_country_ids):
        pts = denom_map.get(cid) or {}
        if len(pts) < min_rows:
            problems.append(f"[c] {label}: {cid} の行数 {len(pts)} < 下限 {min_rows}")
        nonpos = sorted(pr for pr, v in pts.items() if not (v > 0))
        if nonpos:
            problems.append(f"[c] {label}: {cid} に非正値 {len(nonpos)} 件 (例 {nonpos[:3]})")

    return problems


def check_gdp_base_year_canary(
    gdp_map: dict[str, dict[str, float]],
    canary_cfg: dict,
) -> list[str]:
    """
    ゲート f: GDP の基準年が変わっていないかのカナリア。

    米国は購買力平価の numeraire（PPP 換算 = 1）なので、EIA の USA 系列は
    「米実質 GDP を基準年ドルで表したもの」に等しい。実質値は基準年においてのみ
    名目値と一致するため、USA の基準年の値が BEA 名目 GDP と一致するかを見れば
    基準年が保たれているかを判定できる。

    ★ 基準年は unit 文字列（BDOLPPP = billion dollars at purchasing power parities）に
      含まれない。EIA がサイレントにリベースしても unit ゲートでは検知できず、
      公開している単位表記「t-CO2/百万2015年PPPドル」だけが黙って嘘になる。
      2015 → 2017 で約 +2.9%、2015 → 2020 で約 +9.5% 水準がずれるので、
      許容 1%（BEA の年次改定は 10 年前の名目水準では通常 ±0.3% 以内）で弾ける。
    """
    cid = str(canary_cfg["country_region_id"])
    base_year = str(canary_cfg["base_year"])
    ref = float(canary_cfg["reference_nominal_bdol"])
    tol = float(canary_cfg["tolerance_pct"])

    value = (gdp_map.get(cid) or {}).get(base_year)
    if value is None:
        return [f"[f] 基準年カナリア: {cid} の {base_year} 年 GDP が取得できず検証不能"]
    dev = abs(value - ref) / ref * 100.0
    if dev > tol:
        return [
            f"[f] 基準年カナリア破れ: {cid} {base_year} = {value:,.3f} が "
            f"BEA 名目 GDP {ref:,.3f} 十億ドルから {dev:.2f}% 乖離（許容 {tol}%）。"
            f"EIA が GDP の基準年を {base_year} 以外へ変更した疑いが濃い。"
            f"unit（BDOLPPP）は基準年を含まないためこのカナリアでしか検知できない。"
            f"→ source_map.yaml の derived.gdp_base_year_canary と、"
            f"eia-co2-per-gdp-* 6 系列の単位表記を実測し直すこと。"
        ]
    logger.info(
        "[f] 基準年カナリア OK: %s %s = %.3f（BEA 名目 %.3f 十億ドルとの乖離 %.3f%% <= %.1f%%）"
        " → GDP は 2015 年基準・PPP のまま",
        cid, base_year, value, ref, dev, tol,
    )
    return []


def build_derived_series(
    indicators: dict,
    derived_ids: list[str],
    co2_series: dict[str, list[tuple[str, float]]],
    denom_maps: dict[str, dict[str, dict[str, float]]],
    range_gates: dict,
    min_rows: int,
) -> tuple[dict[str, list[tuple[str, float]]], list[str]]:
    """
    派生 12 系列を計算する。戻り値は (series, problems)。

    ゲート d: 年の突合。分母（人口・GDP）は分子（CO2）より 1 年進んでおり
        （2026-08-20 実測: 分母 1980-2025 / CO2 1980-2024）、CO2 に無い period を
        出力すると分子の無い年が混ざる。**分子に存在する period だけ**を採る。
    ゲート e: 派生値のレンジ検証（source_map の derived.range_gates）。
    """
    series: dict[str, list[tuple[str, float]]] = {}
    problems: list[str] = []

    for ind_id in derived_ids:
        cfg = indicators[ind_id]
        kind = str(cfg["derived_kind"])
        if kind not in DERIVED_SCALE:
            problems.append(f"[d] {ind_id}: 未知の derived_kind={kind}")
            continue
        num_id = str(cfg["numerator_id"])
        cid = str(cfg["country_region_id"])
        numerator = dict(co2_series.get(num_id) or [])
        denominator = (denom_maps[DERIVED_DENOM_KEY[kind]] or {}).get(cid) or {}
        if not numerator:
            problems.append(f"[d] {ind_id}: 分子 {num_id} が空")
            continue
        if not denominator:
            problems.append(f"[d] {ind_id}: 分母（{DERIVED_DENOM_KEY[kind]} / {cid}）が空")
            continue

        scale = DERIVED_SCALE[kind]
        lo, hi = (float(x) for x in range_gates[kind])

        # ★ ゲート d: 分子（CO2）に存在する period のみ。分母だけが持つ年は捨てる。
        common = sorted(set(numerator) & set(denominator))
        dropped = sorted(set(denominator) - set(numerator))
        if dropped:
            logger.info(
                "[d] %-26s 分母のみが持つ %d 年を出力しない: %s",
                ind_id, len(dropped), ",".join(dropped[-3:]),
            )

        points: list[tuple[str, float]] = []
        out_of_range: list[tuple[str, float]] = []
        for period in common:
            den = denominator[period]
            if not (den > 0):
                problems.append(f"[d] {ind_id}: {period} の分母が非正値 ({den})")
                continue
            value = numerator[period] / den * scale
            if not (lo <= value <= hi):
                out_of_range.append((period, round(value, 4)))
            points.append((period, round(value, DERIVED_ROUND)))

        # --- ゲート e: レンジ ------------------------------------------------
        if out_of_range:
            problems.append(
                f"[e] {ind_id}: レンジ外 {len(out_of_range)} 件 "
                f"(許容 {lo}〜{hi}, 例 {out_of_range[:3]})"
            )
        # --- 行数下限 ---------------------------------------------------------
        if len(points) < min_rows:
            problems.append(f"[e] {ind_id}: 行数 {len(points)} < 下限 {min_rows}")

        series[ind_id] = points

    return series, problems


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

    # 系列を「API から直接取る CO2 9 本」と「そこから計算する派生 12 本」に分ける。
    # 派生側は derived_kind（per_capita / per_gdp）を持つことで識別する。
    co2_ids = [i for i in indicator_ids if not indicators[i].get("derived_kind")]
    derived_ids = [i for i in indicator_ids if indicators[i].get("derived_kind")]

    # 呼び出し ①: 6 地域 × productId 4008 / ②: JPN × (4002, 4006, 4010)
    total_ids = [i for i in co2_ids if str(indicators[i].get("product_id")) == "4008"]
    breakdown_ids = [i for i in co2_ids if i in JP_BREAKDOWN]

    # 呼び出し ③④: 派生の分母（人口 / 実質GDP）。★ 分母自体は系列化しない。
    derived_cfg = source_cfg["derived"]
    denom_cfgs = derived_cfg["denominators"]
    derived_min_rows = int(derived_cfg["min_rows"])
    denom_country_ids = sorted({
        str(indicators[i]["country_region_id"]) for i in derived_ids
    })

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
        # ★ activityId だけで絞らず productId / unit も必ず明示指定する。
        #   activityId 33 / 34 には productId 47（Energy intensity）が同居しており、
        #   activityId 単独では「エネルギー原単位」を人口・GDP として取り込んでしまう。
        denom_rows: dict[str, list[dict]] = {}
        for denom_key in ("population", "gdp"):
            dc = denom_cfgs[denom_key]
            denom_rows[denom_key] = fetch_all(
                api_base, api_key,
                activity_id=str(dc["activity_id"]),
                product_ids=[str(dc["product_id"])],
                country_region_ids=denom_country_ids,
                unit_filter=str(dc["unit"]),
                raw_dir=raw_dir, today_tag=today_tag, slug=denom_key,
            )
    except Exception as e:
        msg = _redact(str(e), api_key)
        logger.error("fetch failed: %s", msg)
        append_log(log_dir, "fetch_eia_intl", "FAIL", f"fetch failed: {msg[:200]}")
        return 1

    rows = rows1 + rows2
    logger.info("fetched %d rows total (%d + %d)", len(rows), len(rows1), len(rows2))

    series, unknown = rows_to_series(rows, indicators, co2_ids)
    requested_country_ids = {str(indicators[i]["country_region_id"]) for i in co2_ids}

    problems = check_gates(rows, series, unknown, unit_filter, requested_country_ids)

    # --- 派生側: 分母のゲート a / b / c → 基準年カナリア f → 計算（ゲート d / e）---
    denom_country_set = set(denom_country_ids)
    denom_maps: dict[str, dict[str, dict[str, float]]] = {}
    for denom_key in ("population", "gdp"):
        dm = denominator_rows_to_map(denom_rows[denom_key], denom_country_set)
        denom_maps[denom_key] = dm
        problems += check_denominator_gates(
            denom_key, denom_rows[denom_key], denom_cfgs[denom_key],
            dm, denom_country_set, derived_min_rows,
        )
    problems += check_gdp_base_year_canary(
        denom_maps["gdp"], derived_cfg["gdp_base_year_canary"]
    )

    derived_series, derived_problems = build_derived_series(
        indicators, derived_ids, series, denom_maps,
        derived_cfg["range_gates"], derived_min_rows,
    )
    problems += derived_problems
    if problems:
        # ★ 1 本でも破れたら CSV を 1 行も書かずに落とす。
        for p in problems:
            logger.error("GATE FAILED %s", p)
        append_log(
            log_dir, "fetch_eia_intl", "FAIL",
            f"validation gates failed ({len(problems)}): {problems[0][:160]}",
        )
        return 1
    # ★ CO2 側のゲートが全部通ってから派生をマージする。
    #   分母が 1 本でも欠けた状態で書き出すと、派生 CSV に歯抜けの年が残る。
    series.update(derived_series)
    logger.info(
        "all validation gates passed "
        "(CO2 a: facet 一致 / b: 加算整合 / c: 行数・正値 | "
        "派生 a: productId / b: countryRegionId / c: unit・行数 / "
        "d: 年の突合 / e: レンジ / f: 基準年カナリア)"
    )

    if args.dry_run:
        for ind_id in indicator_ids:
            pts = sorted(series[ind_id])
            logger.info(
                "[dry-run] %-26s n=%-3d %s..%s latest=%.3f %s",
                ind_id, len(pts), pts[0][0], pts[-1][0], pts[-1][1],
                indicators[ind_id].get("unit", ""),
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
            "%-26s n=%-3d %s..%s latest=%.3f %s",
            ind_id, len(df), df["date"].iloc[0], df["date"].iloc[-1],
            df["value"].iloc[-1], ind_cfg.get("unit", ""),
        )

    summary = (
        f"series={len(written)} (CO2 {len(co2_ids)} + derived {len(derived_ids)}) "
        f"rows={total_rows}"
    )
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_eia_intl", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
