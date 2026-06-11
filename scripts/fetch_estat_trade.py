"""
e-Stat API（政府統計の総合窓口）から 財務省 普通貿易統計「品別国別表（輸入）」を取得し、
日本のエネルギー輸入 源国別 金額（原油 / LNG / 石炭、年次）を geopolitics ドメイン系列にする。

目的（地政 geopolitics seed）:
    日本のエネルギー輸入の「源国集中（中東依存度 等）」を可視化するための、
    原油・LNG・石炭の輸入相手国別 年間金額（千円）。

系列設計（id）:
    jp-import-value-{crude|lng|coal}-{cc}   相手国別 年間輸入額（region=ISO2 大文字）
    jp-import-value-{crude|lng|coal}-total  全相手国合算（region=WORLD、源国集中の分母）
    domain=geopolitics / frequency=annual / unit=千円 / date=YYYY-01-01。

データ源（source_map.yaml の `estat-trade` セクション）:
    普通貿易統計「確定/確速 品別国別表（輸入）」年次統計表を結合。
      - 0003313966 : 2016-2020（確定）
      - 0003425294 : 2021-2024（確定）+ 2025（確々報）
    次元（getMetaInfo 検証 2026-06-11、L-062）:
      - cat01 : 統計品目表(輸入) 9 桁 HS コード（@name=@code）。8884〜8986 件。
      - cat02 : 数量・額。コード 140 = 合計_金額（単位=千円、年間合計）。
      - area  : 国（コード 5XXXX、@name="NNN_国名"）。232 件前後。
      - time  : 時間軸(年次)。表ごとに 5 年分。
    各燃料の輸入額は HS 見出し配下の全 9 桁コードを合算する（prefix で実行時導出 = HS 改正耐性）:
      原油 = 2709 / LNG = 271111 / 石炭 = 2701。

方式:
    1) 表ごとに getMetaInfo で cat01 コードを取得し、燃料 prefix にマッチする HS コードを抽出。
    2) 燃料ごとに getStatsData（cdCat01=該当 HS コード群, cdCat02=140）を 1 回叩く。
       e-Stat は値が存在する (品目 × 国 × 年) のみ返す（疎）ため応答は小さい。
    3) (area, year) 単位で HS コードを跨いで金額を合算 → 相手国別 年間輸入額。
    4) 対象相手国は系列化、全相手国合算を total 系列にする。

認証:
    環境変数 `ESTAT_APP_ID`（fetch_estat.py と同一の 40 桁キー）。未設定時はログのみ吐いて exit 1。

出力（D-017: processed_dir=geopolitics）:
    - data/raw/estat_trade/{fuel}_{stats_data_id}_{YYYYMMDD}.json
    - data/processed/geopolitics/{indicator_id}.csv / .parquet / .metadata.json

ライセンス（L-063 = GO）:
    財務省 貿易統計（普通貿易統計）。政府標準利用規約 2.0 互換、出典明示で商用含む再利用可。
    license id は既存 estat-terms を流用。license_notice: "出典: 財務省 貿易統計（政府統計の総合窓口 e-Stat）。"

参考:
    - API 仕様: https://www.e-stat.go.jp/api/api-info/e-stat-manual3-0
    - 2026-06-11 実 API（getStatsList searchWord「貿易統計 品別国別 輸入」+ getMetaInfo）で
      統計表 ID・HS コード・国コード・cat02 金額項目を確定（L-062）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from scripts.common.http import get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_estat_trade")

SOURCE_KEY = "estat-trade"
# NaN 扱いする特殊値（e-Stat 慣習）
NAN_TOKENS = {"-", "...", "", "X", "x", "***", "NA"}


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_app_id() -> str:
    key = (os.environ.get("ESTAT_APP_ID") or "").strip()
    if not key or key.lower() in {"your_app_id_here", "changeme", "todo"}:
        return ""
    return key


def _class_objs(payload_root: dict) -> list[dict]:
    """getMetaInfo / getStatsData の CLASS_OBJ 配列を返す。"""
    cos = (
        payload_root.get("METADATA_INF", payload_root.get("STATISTICAL_DATA", {}))
        .get("CLASS_INF", {})
        .get("CLASS_OBJ", [])
    )
    if isinstance(cos, dict):
        cos = [cos]
    return cos


def fetch_meta(api_base: str, app_id: str, stats_data_id: str) -> dict:
    """getMetaInfo を叩いて JSON を返す。"""
    params = {"appId": app_id, "statsDataId": stats_data_id, "lang": "J"}
    url = f"{api_base.rstrip('/')}/getMetaInfo"
    logger.info("GET %s statsDataId=%s", url, stats_data_id)
    r = get(url, params=params, timeout=120)
    r.raise_for_status()
    return r.json()


def hs_codes_for_fuel(meta_json: dict, prefixes: list[str]) -> list[str]:
    """getMetaInfo の cat01（統計品目表）から prefix にマッチする 9 桁 HS コードを抽出。"""
    cos = _class_objs(meta_json.get("GET_META_INFO", {}))
    codes: list[str] = []
    for co in cos:
        if co.get("@id") != "cat01":
            continue
        cls = co.get("CLASS", [])
        if isinstance(cls, dict):
            cls = [cls]
        for c in cls:
            code = str(c.get("@code", ""))
            if any(code.startswith(p) for p in prefixes):
                codes.append(code)
    return codes


def fetch_stats_data(
    api_base: str,
    app_id: str,
    stats_data_id: str,
    *,
    cd_cat01: str,
    cd_cat02: str,
) -> bytes:
    """getStatsData を叩いて生 JSON バイト列を返す（cdCat01 はカンマ区切り複数コード可）。"""
    params = {
        "appId": app_id,
        "statsDataId": stats_data_id,
        "lang": "J",
        "metaGetFlg": "Y",
        "cdCat01": cd_cat01,
        "cdCat02": cd_cat02,
    }
    url = f"{api_base.rstrip('/')}/getStatsData"
    safe = {k: ("***" if k == "appId" else v) for k, v in params.items()}
    logger.info("GET %s params=%s", url, safe)
    r = get(url, params=params, timeout=120)
    r.raise_for_status()
    return r.content


def build_time_year_map(class_objs: list[dict]) -> dict[str, int]:
    """time 次元コード -> 西暦年（int）。"確速 品別国別表" は @code が "YYYY000000"。"""
    mp: dict[str, int] = {}
    for co in class_objs:
        if co.get("@id") != "time":
            continue
        cls = co.get("CLASS", [])
        if isinstance(cls, dict):
            cls = [cls]
        for c in cls:
            code = str(c.get("@code", ""))
            name = str(c.get("@name", ""))
            year = None
            # name 例 "2025年" を優先、無ければ code 先頭 4 桁
            for token in (name, code):
                digits = "".join(ch for ch in token if ch.isdigit())
                if len(digits) >= 4:
                    try:
                        y = int(digits[:4])
                        if 1900 <= y <= 2100:
                            year = y
                            break
                    except ValueError:
                        pass
            if code and year is not None:
                mp[code] = year
    return mp


def collect_area_year_values(
    payload: dict,
    cd_cat02: str,
) -> dict[tuple[str, int], float]:
    """
    1 燃料・1 統計表分の getStatsData payload を (area_code, year) -> 金額合計（千円）に集約。
    HS コード（cat01）を跨いで合算する。cat02 は cd_cat02（合計_金額）のみ採用。
    """
    sd = payload.get("GET_STATS_DATA", {}).get("STATISTICAL_DATA", {})
    class_objs = _class_objs({"STATISTICAL_DATA": sd})
    year_map = build_time_year_map(class_objs)

    values = sd.get("DATA_INF", {}).get("VALUE", [])
    if isinstance(values, dict):
        values = [values]

    out: dict[tuple[str, int], float] = {}
    for v in values:
        # cd_cat02 で getStatsData 済みだが、念のため明示フィルタ（cat02 が複数返る事故を防ぐ）
        if str(v.get("@cat02")) != str(cd_cat02):
            continue
        area = str(v.get("@area", ""))
        year = year_map.get(str(v.get("@time", "")))
        if not area or year is None:
            continue
        raw = v.get("$", "")
        if raw is None or str(raw).strip() in NAN_TOKENS:
            continue
        try:
            val = float(str(raw).replace(",", ""))
        except (ValueError, TypeError):
            continue
        out[(area, year)] = out.get((area, year), 0.0) + val
    return out


def process_trade(
    cfg: dict,
    app_id: str,
    raw_dir: Path,
    log_dir: Path,
    today_tag: str,
    wanted: set[str] | None,
) -> tuple[list[str], int]:
    """estat-trade セクション: 燃料 × 相手国の輸入額系列を geopolitics ドメインへ書き出す。"""
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が無い — スキップ", SOURCE_KEY)
        return [], 0

    api_base = source_cfg["api_base"]
    source_url = source_cfg.get("source_url", "")
    domain = source_cfg.get("domain", "geopolitics")
    processed_dir = ROOT / "data" / "processed" / domain
    cd_cat02 = str(source_cfg.get("value_cat02_code", "140"))

    tables = [t.get("stats_data_id") for t in (source_cfg.get("tables") or []) if t.get("stats_data_id")]
    fuels: dict = source_cfg.get("fuels") or {}
    country_codes: dict = source_cfg.get("country_codes") or {}
    fuel_countries: dict = source_cfg.get("fuel_countries") or {}
    if not (tables and fuels and country_codes and fuel_countries):
        logger.error("%s: tables/fuels/country_codes/fuel_countries の設定不足 — 停止", SOURCE_KEY)
        append_log(log_dir, "fetch_estat_trade", "FAIL", "config incomplete")
        return [], 0

    # 燃料 -> {(area, year): 金額合計} を全統計表で蓄積
    fuel_agg: dict[str, dict[tuple[str, int], float]] = {f: {} for f in fuels}

    for sid in tables:
        try:
            meta_json = fetch_meta(api_base, app_id, str(sid))
        except Exception as e:
            logger.exception("%s: getMetaInfo failed: %s", sid, e)
            append_log(log_dir, "fetch_estat_trade", "WARN", f"{sid} getMetaInfo failed: {e}")
            continue
        meta_status = meta_json.get("GET_META_INFO", {}).get("RESULT", {}).get("STATUS")
        if meta_status not in (0, "0"):
            logger.error("%s: getMetaInfo STATUS=%s — skip", sid, meta_status)
            append_log(log_dir, "fetch_estat_trade", "FAIL", f"{sid} getMetaInfo STATUS={meta_status}")
            continue

        for fuel, fcfg in fuels.items():
            prefixes = list(fcfg.get("hs_prefixes") or [])
            codes = hs_codes_for_fuel(meta_json, prefixes)
            if not codes:
                logger.warning("%s/%s: prefix %s にマッチする HS コードなし — skip", sid, fuel, prefixes)
                continue
            try:
                content = fetch_stats_data(
                    api_base, app_id, str(sid),
                    cd_cat01=",".join(codes), cd_cat02=cd_cat02,
                )
            except Exception as e:
                logger.exception("%s/%s: getStatsData failed: %s", sid, fuel, e)
                append_log(log_dir, "fetch_estat_trade", "WARN", f"{sid}/{fuel} fetch failed: {e}")
                continue
            save_raw(content, raw_dir, f"{fuel}_{sid}_{today_tag}.json")
            try:
                payload = json.loads(content.decode("utf-8"))
            except Exception as e:
                logger.exception("%s/%s: json decode failed: %s", sid, fuel, e)
                continue
            result = payload.get("GET_STATS_DATA", {}).get("RESULT", {})
            if result.get("STATUS") not in (0, "0"):
                logger.error("%s/%s: getStatsData STATUS=%s msg=%r", sid, fuel,
                             result.get("STATUS"), result.get("ERROR_MSG"))
                append_log(log_dir, "fetch_estat_trade", "FAIL",
                           f"{sid}/{fuel} STATUS={result.get('STATUS')}")
                continue
            agg = collect_area_year_values(payload, cd_cat02)
            for k, val in agg.items():
                fuel_agg[fuel][k] = fuel_agg[fuel].get(k, 0.0) + val
            logger.info("%s/%s: HS=%d codes, %d (area,year) values", sid, fuel, len(codes), len(agg))

    # --- 系列の構築（相手国別 + total） ---
    written: list[str] = []
    total_rows = 0
    summary_top: dict[str, str] = {}

    for fuel, agg in fuel_agg.items():
        if not agg:
            logger.warning("%s: 0 values across tables — skip", fuel)
            continue
        fuel_name = (fuels.get(fuel) or {}).get("name_ja", fuel)
        target_codes = [str(c) for c in (fuel_countries.get(fuel) or [])]

        # total（全相手国合算、region=WORLD）
        total_by_year: dict[int, float] = {}
        for (area, year), val in agg.items():
            total_by_year[year] = total_by_year.get(year, 0.0) + val

        series: list[tuple[str, str, dict]] = []  # (indicator_id, region, {year: value})
        total_id = f"jp-import-value-{fuel}-total"
        series.append((total_id, "WORLD",
                       {y: total_by_year[y] for y in total_by_year}))

        for area in target_codes:
            cc = country_codes.get(area)
            if not cc:
                logger.warning("%s: country_codes に area=%s が無い — skip", fuel, area)
                continue
            by_year = {year: val for (a, year), val in agg.items() if a == area}
            if not by_year:
                logger.warning("%s/%s(%s): 値なし — skip", fuel, cc, area)
                continue
            ind_id = f"jp-import-value-{fuel}-{cc.lower()}"
            series.append((ind_id, cc, by_year))

        for ind_id, region, by_year in series:
            if wanted is not None and ind_id not in wanted:
                continue
            rows = [
                {"date": f"{year:04d}-01-01", "indicator_id": ind_id,
                 "region": region, "value": val, "source_url": source_url}
                for year, val in sorted(by_year.items())
            ]
            df = pd.DataFrame(rows, columns=["date", "indicator_id", "region", "value", "source_url"])
            if df.empty:
                continue
            # --- メタ用に name/notes を注入した source_cfg を組む ---
            if region == "WORLD":
                disp = f"日本 {fuel_name} 輸入額 全相手国合計"
                note = (f"財務省 貿易統計 品別国別表（輸入）の {fuel_name} 全相手国合算（年間金額・千円）。"
                        f"源国集中・依存度の分母。HS 見出し配下の全コードを合算。")
            else:
                disp = f"日本 {fuel_name} 輸入額 {region}"
                note = (f"財務省 貿易統計 品別国別表（輸入）の {fuel_name} 相手国 {region} 別 年間金額（千円）。"
                        f"HS 見出し配下の全コードを合算。")
            local_cfg = dict(source_cfg)
            local_cfg["indicators"] = {
                ind_id: {
                    "name": disp, "domain": domain, "unit": source_cfg.get("unit", "千円"),
                    "aggregation": source_cfg.get("aggregation", "annual_sum"),
                    "backfill_start": source_cfg.get("backfill_start"),
                    "notes": note, "depends_on": None,
                }
            }
            write_processed(df, processed_dir, basename=ind_id)
            write_metadata_for_indicator(processed_dir, local_cfg, ind_id, df)
            written.append(ind_id)
            total_rows += len(df)

        # ログ用: latest year の上位を記録
        latest = max(total_by_year)
        tot = total_by_year[latest] or 1.0
        ranked = sorted(
            ((area, val) for (area, yy), val in agg.items() if yy == latest),
            key=lambda x: -x[1],
        )[:3]
        top = ", ".join(f"{country_codes.get(a, a)}={v/tot*100:.0f}%" for a, v in ranked)
        summary_top[fuel] = (f"{latest} total={tot/1e9:.2f}兆円 top[{top}]")

    if not written:
        logger.error("estat-trade: no series produced rows")
        append_log(log_dir, "fetch_estat_trade", "FAIL", "no series produced rows")
        return [], 0

    for fuel, s in summary_top.items():
        logger.info("SUMMARY %s: %s", fuel, s)
    logger.info("estat-trade: %d series, %d rows -> %s", len(written), total_rows, processed_dir)
    append_log(log_dir, "fetch_estat_trade", "OK", f"series={len(written)} rows={total_rows}")
    return written, total_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Japan energy import value by source country from e-Stat 普通貿易統計 (geopolitics)"
    )
    parser.add_argument(
        "--series", type=str, default=None,
        help="カンマ区切りで indicator_id を絞る（例: jp-import-value-crude-sa,jp-import-value-crude-total）",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="全期間取得（e-Stat 側が表ごとに全量返すため互換用フラグ）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    app_id = _get_app_id()
    raw_dir = ROOT / "data" / "raw" / "estat_trade"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    if not app_id:
        logger.error(
            "ESTAT_APP_ID is not set. e-Stat API requires registration "
            "(https://www.e-stat.go.jp/api/). Skipping fetch."
        )
        append_log(log_dir, "fetch_estat_trade", "WARN", "ESTAT_APP_ID missing — skipped")
        return 1

    wanted: set[str] | None = None
    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}

    written, total_rows = process_trade(cfg, app_id, raw_dir, log_dir, today_tag, wanted)
    if not written:
        return 1

    logger.info("done: series=%d rows=%d", len(written), total_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
