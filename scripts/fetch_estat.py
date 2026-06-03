"""
e-Stat API（政府統計の総合窓口）から日本の公的統計系列を取得するスクリプト。

対象 A（Phase 2 international batch 2、source_map.yaml の `estat` セクション / economy ドメイン）:
    - jpn-cpi-yoy              : 2020 年基準 消費者物価指数 総合 全国 前年同月比(%)
                                 statsDataId=0003427113 / tab=3 / cat01=0001 / area=00000
    - jpn-industrial-production : 鉱工業生産・出荷・在庫指数 2020 年基準 業種別原指数【月次】 鉱工業計
                                 statsDataId=0004015804 / cat01=0001000

対象 B（source_map.yaml の `estat-population` セクション / population ドメイン、年次）:
    人口推計 都道府県別 3 指標 × 47 都道府県 = 141 系列。
      - jpn-pop-total-{cc}    総人口（年齢3区分=総数）
      - jpn-pop-working-{cc}  生産年齢人口（年齢3区分=15～64歳）
      - jpn-pop-65over-{cc}   65 歳以上人口（年齢3区分=65歳以上）
    統計表（各年10月1日現在、男女計・総人口、単位=千人）:
      - 0004021110 : 2016-2020（cat02=人口 / cat03=年齢3区分）
      - 0003448225 : 2021-2024（cat02=年齢3区分 / cat03=人口）  ← cat 割当が表で入替
    分類コード（getMetaInfo 検証 2026-06-03）: 男女計=000 / 総人口=001 /
      年齢3区分 総数=000・15～64歳=002・65歳以上=003。area={cc}000（cc=JIS 都道府県コード）。
    cdCatNN の位置は表で入れ替わるため、実行時に分類「名」で次元 @id を解決する（L-062）。

方式:
    GET https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData
    params: appId, statsDataId, cdTab, cdCat01, cdArea, lang=J（economy は分類で絞る、
    population は表全体を 1 リクエストで取り Python 側で 141 系列に分解）

認証:
    環境変数 `ESTAT_APP_ID` を python-dotenv 経由で .env から読み込む。
    無料登録（https://www.e-stat.go.jp/api/）で発行される 40 桁のキー。
    未設定時はワーキング系列なしでログのみ吐いて exit 1（FRED と異なり CSV フォールバックは無い）。

出力（D-017: processed_dir は指標の domain に連動）:
    - data/raw/estat/{indicator_id}_{YYYYMMDD}.json     (getStatsData 生レス)
    - data/processed/{domain}/{indicator_id}.csv        (共通スキーマ long 形式)
    - data/processed/{domain}/{indicator_id}.parquet
    - data/processed/{domain}/{indicator_id}.metadata.json (D-011)
    economy: CPI/IIP（後方互換）。population: 人口推計 都道府県別 141 系列。

ライセンス:
    e-Stat 利用規約。政府標準利用規約 2.0 互換、出典明示で再利用可。
    license_notice: "出典: 政府統計の総合窓口（e-Stat）。"

参考:
    - API 仕様: https://www.e-stat.go.jp/api/api-info/e-stat-manual3-0
    - 2026-05-27 実 API で CPI/IIP の統計表 ID と分類コードを確定（L-062）。
    - 2026-06-03 実 API で人口推計 都道府県別の統計表 ID と分類コードを確定（L-062）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
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
logger = logging.getLogger("fetch_estat")

SOURCE_KEY = "estat"
# 人口推計 都道府県別（年次、population ドメイン）の専用セクション
POP_SOURCE_KEY = "estat-population"

# 「2026年4月」「2025年12月」など CPI 時間軸 name のパターン
CPI_TIME_RE = re.compile(r"^(\d{4})年(\d{1,2})月$")
# 「201801」「202502」など IIP 時間軸 name のパターン
IIP_TIME_RE = re.compile(r"^(\d{4})(\d{2})$")
# 「2021年10月1日現在」など 人口推計 年次の時間軸 name から西暦年を抜く
POP_YEAR_RE = re.compile(r"(\d{4})年")
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


def fetch_stats_data(
    api_base: str,
    app_id: str,
    stats_data_id: str,
    *,
    cd_tab: str | None = None,
    cd_cat01: str | None = None,
    cd_area: str | None = None,
) -> bytes:
    """getStatsData を叩いて生 JSON バイト列を返す。"""
    params: dict[str, str] = {
        "appId": app_id,
        "statsDataId": stats_data_id,
        "lang": "J",
        "metaGetFlg": "Y",
    }
    if cd_tab:
        params["cdTab"] = cd_tab
    if cd_cat01:
        params["cdCat01"] = cd_cat01
    if cd_area:
        params["cdArea"] = cd_area
    url = f"{api_base.rstrip('/')}/getStatsData"
    safe_params = {k: ("***" if k == "appId" else v) for k, v in params.items()}
    logger.info("GET %s params=%s", url, safe_params)
    r = get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.content


def build_time_name_map(payload: dict) -> dict[str, str]:
    """時間軸コード -> 表示名（"YYYYMM" 等）の辞書を構築。"""
    class_objs = (
        payload.get("GET_STATS_DATA", {})
        .get("STATISTICAL_DATA", {})
        .get("CLASS_INF", {})
        .get("CLASS_OBJ", [])
    )
    if isinstance(class_objs, dict):
        class_objs = [class_objs]
    mp: dict[str, str] = {}
    for co in class_objs:
        if co.get("@id") != "time":
            continue
        cls = co.get("CLASS", [])
        if isinstance(cls, dict):
            cls = [cls]
        for c in cls:
            code = c.get("@code", "")
            name = c.get("@name", "")
            if code:
                mp[code] = name
    return mp


def parse_time_name(name: str) -> str | None:
    """時間軸 name を ISO 月初日 (YYYY-MM-01) に変換。月次でなければ None。"""
    if not name:
        return None
    m = CPI_TIME_RE.match(name)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        return f"{year:04d}-{month:02d}-01"
    m = IIP_TIME_RE.match(name)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}-01"
    return None


def parse_values(payload: dict, time_map: dict[str, str]) -> pd.DataFrame:
    """VALUE 配列を (_iso_date, value) の DataFrame に変換。月次以外は捨てる。"""
    values = (
        payload.get("GET_STATS_DATA", {})
        .get("STATISTICAL_DATA", {})
        .get("DATA_INF", {})
        .get("VALUE", [])
    )
    if isinstance(values, dict):
        values = [values]
    rows: list[tuple[str, float]] = []
    for v in values:
        time_code = v.get("@time", "")
        raw = v.get("$", "")
        # 数値文字列のパース。特殊トークンは NaN として落とす
        if raw is None or str(raw).strip() in NAN_TOKENS:
            continue
        try:
            val = float(str(raw).replace(",", ""))
        except (ValueError, TypeError):
            continue
        name = time_map.get(time_code, "")
        iso = parse_time_name(name)
        if iso is None:
            continue
        rows.append((iso, val))
    if not rows:
        return pd.DataFrame(columns=["_iso_date", "value"])
    df = pd.DataFrame(rows, columns=["_iso_date", "value"])
    df["_iso_date"] = pd.to_datetime(df["_iso_date"])
    df = df.dropna(subset=["_iso_date"]).sort_values("_iso_date").reset_index(drop=True)
    df = df.drop_duplicates(subset=["_iso_date"], keep="last").reset_index(drop=True)
    return df


def normalize_to_long(df: pd.DataFrame, indicator_id: str, source_url: str) -> pd.DataFrame:
    cols = ["date", "indicator_id", "region", "value", "source_url"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    out = df.copy()
    out["date"] = out["_iso_date"].dt.strftime("%Y-%m-%d")
    out["indicator_id"] = indicator_id
    out["region"] = "jp"
    out["source_url"] = source_url
    return out[cols].sort_values("date").reset_index(drop=True)


# --- 人口推計 都道府県別（population ドメイン） -----------------------------


def _stats_data_class_objs(payload: dict) -> list[dict]:
    """getStatsData レスポンスの CLASS_OBJ 配列（次元定義）を返す。"""
    cos = (
        payload.get("GET_STATS_DATA", {})
        .get("STATISTICAL_DATA", {})
        .get("CLASS_INF", {})
        .get("CLASS_OBJ", [])
    )
    if isinstance(cos, dict):
        cos = [cos]
    return cos


def resolve_pop_dims(class_objs: list[dict]) -> dict[str, str]:
    """
    人口推計 統計表の次元 @id を「分類名」で解決する（L-062: cdCatNN の位置を推測しない）。

    同じ系列（男女別 / 人口 総数・日本人 / 年齢3区分）でも統計表により cat02/cat03 の割当が
    入れ替わる（0004021110 は cat02=人口・cat03=年齢、0003448225 は cat02=年齢・cat03=人口）。
    位置ではなく分類名で sex/poptype/age/area/time の @id を引き当てることで取り違いを防ぐ。
    """
    dims: dict[str, str] = {}
    for co in class_objs:
        cid = co.get("@id", "")
        name = (co.get("@name") or "").strip()
        if cid == "time":
            dims["time"] = cid
        elif "男女" in name:
            dims["sex"] = cid
        elif name == "人口":
            dims["poptype"] = cid
        elif "年齢" in name:
            dims["age"] = cid
        elif "都道府県" in name or cid == "area":
            dims["area"] = cid
    return dims


def build_pop_time_map(class_objs: list[dict], time_id: str) -> dict[str, str]:
    """時間軸コード -> 表示名（"YYYY年10月1日現在"）の辞書。"""
    mp: dict[str, str] = {}
    for co in class_objs:
        if co.get("@id") != time_id:
            continue
        cls = co.get("CLASS", [])
        if isinstance(cls, dict):
            cls = [cls]
        for c in cls:
            code = c.get("@code", "")
            if code:
                mp[code] = c.get("@name", "")
    return mp


def parse_pop_year(name: str) -> str | None:
    """"2021年10月1日現在" -> "2021-10-01"（各年10月1日現在が観測基準日）。"""
    if not name:
        return None
    m = POP_YEAR_RE.search(name)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-10-01"


def collect_pop_rows(
    payload: dict,
    source_cfg: dict,
) -> dict[str, list[tuple[str, str, float]]]:
    """
    1 統計表分の getStatsData payload を 141 系列の行に分解する。
    返り値: indicator_id -> [(date, region, value), ...]
    """
    class_objs = _stats_data_class_objs(payload)
    dims = resolve_pop_dims(class_objs)
    missing = {"sex", "poptype", "age", "area", "time"} - dims.keys()
    if missing:
        raise ValueError(f"人口推計 次元の名前解決に失敗: missing={missing} got={dims}")
    time_map = build_pop_time_map(class_objs, dims["time"])

    metrics = source_cfg.get("metrics") or {}
    age_to_metric = {str(m["age_code"]): mk for mk, m in metrics.items()}
    indicators = source_cfg.get("indicators") or {}
    key_to_id = {
        (str(v.get("area_code")), v.get("metric")): k for k, v in indicators.items()
    }
    sex_code = str(source_cfg.get("sex_code", "000"))
    poptype_code = str(source_cfg.get("poptype_code", "001"))

    values = (
        payload.get("GET_STATS_DATA", {})
        .get("STATISTICAL_DATA", {})
        .get("DATA_INF", {})
        .get("VALUE", [])
    )
    if isinstance(values, dict):
        values = [values]

    sex_key = f"@{dims['sex']}"
    pop_key = f"@{dims['poptype']}"
    age_key = f"@{dims['age']}"
    area_key = f"@{dims['area']}"
    time_key = f"@{dims['time']}"

    out: dict[str, list[tuple[str, str, float]]] = {}
    for v in values:
        if str(v.get(sex_key)) != sex_code or str(v.get(pop_key)) != poptype_code:
            continue
        metric = age_to_metric.get(str(v.get(age_key)))
        if metric is None:
            continue
        ind_id = key_to_id.get((str(v.get(area_key)), metric))
        if ind_id is None:
            continue  # 全国(00000) 等 141 系列に含まれない area は対象外
        iso = parse_pop_year(time_map.get(str(v.get(time_key)), ""))
        if iso is None:
            continue
        raw = v.get("$", "")
        if raw is None or str(raw).strip() in NAN_TOKENS:
            continue
        try:
            val = float(str(raw).replace(",", ""))
        except (ValueError, TypeError):
            continue
        region = indicators[ind_id].get("region", "")
        out.setdefault(ind_id, []).append((iso, region, val))
    return out


def process_population(
    cfg: dict,
    app_id: str,
    raw_dir: Path,
    log_dir: Path,
    today_tag: str,
    wanted: set[str] | None,
) -> tuple[list[str], int]:
    """
    estat-population セクション: tables の各統計表を 1 回ずつ取得し、
    47 都道府県 × 3 指標 = 141 系列に分解して population ドメインへ書き出す。
    write_processed が date で dedup するため、複数統計表をまたいで append しても安全。
    """
    try:
        source_cfg = cfg["sources"][POP_SOURCE_KEY]
    except KeyError:
        logger.info("source_map.yaml に %s が無いため population をスキップ", POP_SOURCE_KEY)
        return [], 0

    api_base = source_cfg["api_base"]
    source_url = source_cfg.get("source_url", "")
    domain = source_cfg.get("domain", "population")
    processed_dir = ROOT / "data" / "processed" / domain  # D-017: domain 連動
    tables = source_cfg.get("tables") or []
    if not tables:
        logger.warning("%s: tables 未定義 — skip", POP_SOURCE_KEY)
        return [], 0

    accumulated: dict[str, list[tuple[str, str, float]]] = {}
    for tbl in tables:
        sid = tbl.get("stats_data_id") if isinstance(tbl, dict) else tbl
        if not sid:
            continue
        try:
            content = fetch_stats_data(api_base, app_id, str(sid))
        except Exception as e:
            logger.exception("population %s: fetch failed: %s", sid, e)
            append_log(log_dir, "fetch_estat", "WARN", f"population {sid} fetch failed: {e}")
            continue
        save_raw(content, raw_dir, f"jpn-pop_{sid}_{today_tag}.json")
        try:
            payload = json.loads(content.decode("utf-8"))
        except Exception as e:
            logger.exception("population %s: json decode failed: %s", sid, e)
            continue
        result = payload.get("GET_STATS_DATA", {}).get("RESULT", {})
        status = result.get("STATUS")
        if status not in (0, "0"):
            logger.error("population %s: STATUS=%s msg=%r", sid, status, result.get("ERROR_MSG"))
            append_log(log_dir, "fetch_estat", "FAIL", f"population {sid} STATUS={status}")
            continue
        try:
            rows = collect_pop_rows(payload, source_cfg)
        except ValueError as e:
            logger.error("population %s: %s", sid, e)
            append_log(log_dir, "fetch_estat", "FAIL", f"population {sid} {e}")
            continue
        n = sum(len(r) for r in rows.values())
        logger.info("population %s: %d series x rows parsed (%d values)", sid, len(rows), n)
        for ind_id, rws in rows.items():
            accumulated.setdefault(ind_id, []).extend(rws)

    if not accumulated:
        logger.warning("population: 0 rows parsed across all tables")
        return [], 0

    written: list[str] = []
    total_rows = 0
    for ind_id in sorted(accumulated):
        if wanted is not None and ind_id not in wanted:
            continue
        df = pd.DataFrame(accumulated[ind_id], columns=["date", "region", "value"])
        df["indicator_id"] = ind_id
        df["source_url"] = source_url
        df = df[["date", "indicator_id", "region", "value", "source_url"]]
        df = df.sort_values("date").drop_duplicates(["date"], keep="last").reset_index(drop=True)
        write_processed(df, processed_dir, basename=ind_id)
        write_metadata_for_indicator(processed_dir, source_cfg, ind_id, df)
        written.append(ind_id)
        total_rows += len(df)

    if written:
        logger.info(
            "population: %d series, %d rows -> %s (range=%s)",
            len(written), total_rows, processed_dir,
            f"{min(accumulated[written[0]])[0]}..{max(accumulated[written[0]])[0]}",
        )
        append_log(log_dir, "fetch_estat", "OK",
                   f"population series={len(written)} rows={total_rows}")
    return written, total_rows


# --- e-Stat economy（CPI / IIP、月次） --------------------------------------


def process_estat(
    cfg: dict,
    app_id: str,
    raw_dir: Path,
    log_dir: Path,
    today_tag: str,
    wanted: set[str] | None,
) -> tuple[list[str], int]:
    """estat セクション（CPI / IIP、economy ドメイン）。D-017: processed_dir を domain に連動。"""
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.info("source_map.yaml に %s が無いため estat(CPI/IIP) をスキップ", SOURCE_KEY)
        return [], 0

    indicators_cfg: dict = source_cfg.get("indicators") or {}
    if wanted is not None:
        indicators_cfg = {k: v for k, v in indicators_cfg.items() if k in wanted}
    if not indicators_cfg:
        return [], 0

    api_base = source_cfg["api_base"]
    source_url = source_cfg.get("source_url", "")

    written: list[str] = []
    total_rows = 0

    for indicator_id, ind_cfg in indicators_cfg.items():
        stats_data_id = ind_cfg.get("stats_data_id")
        if not stats_data_id:
            logger.warning("%s: stats_data_id が未定義 — skip", indicator_id)
            continue

        # D-017: processed_dir を指標の domain に連動（CPI/IIP=economy、後方互換維持）
        domain = ind_cfg.get("domain") or source_cfg.get("domain") or "economy"
        processed_dir = ROOT / "data" / "processed" / domain

        try:
            content = fetch_stats_data(
                api_base, app_id, stats_data_id,
                cd_tab=ind_cfg.get("cd_tab"),
                cd_cat01=ind_cfg.get("cd_cat01"),
                cd_area=ind_cfg.get("cd_area"),
            )
        except Exception as e:
            logger.exception("%s: fetch failed: %s", indicator_id, e)
            append_log(log_dir, "fetch_estat", "WARN", f"{indicator_id} fetch failed: {e}")
            continue

        save_raw(content, raw_dir, f"{indicator_id}_{today_tag}.json")

        try:
            payload = json.loads(content.decode("utf-8"))
        except Exception as e:
            logger.exception("%s: json decode failed: %s", indicator_id, e)
            continue

        # API レベルのエラー検出
        result = payload.get("GET_STATS_DATA", {}).get("RESULT", {})
        status = result.get("STATUS")
        if status not in (0, "0"):
            logger.error(
                "%s: e-Stat API returned non-zero STATUS=%s msg=%r",
                indicator_id, status, result.get("ERROR_MSG"),
            )
            append_log(log_dir, "fetch_estat", "FAIL",
                       f"{indicator_id} STATUS={status} {result.get('ERROR_MSG')}")
            continue

        time_map = build_time_name_map(payload)
        raw_df = parse_values(payload, time_map)
        if raw_df.empty:
            logger.warning("%s: parsed 0 monthly rows — skip", indicator_id)
            continue

        long_df = normalize_to_long(raw_df, indicator_id, source_url)
        if long_df.empty:
            logger.warning("%s: 0 rows after normalize — skip", indicator_id)
            continue

        write_processed(long_df, processed_dir, basename=indicator_id)
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, long_df)
        written.append(indicator_id)
        total_rows += len(long_df)
        logger.info(
            "%s: %d rows (range=%s..%s, stats_data_id=%s, domain=%s)",
            indicator_id, len(long_df),
            long_df["date"].min(), long_df["date"].max(),
            stats_data_id, domain,
        )

    return written, total_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Japan public statistics from e-Stat API "
                    "(CPI/IIP economy + 人口推計 都道府県別 population)"
    )
    parser.add_argument(
        "--series", type=str, default=None,
        help="カンマ区切りで indicator_id を絞る（例: jpn-cpi-yoy,jpn-pop-total-13）",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="全期間取得（e-Stat 側が表に応じて全量返すため互換用フラグ）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    app_id = _get_app_id()
    raw_dir = ROOT / "data" / "raw" / "estat"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    if not app_id:
        logger.error(
            "ESTAT_APP_ID is not set. e-Stat API requires registration "
            "(https://www.e-stat.go.jp/api/). Skipping fetch."
        )
        append_log(log_dir, "fetch_estat", "WARN", "ESTAT_APP_ID missing — skipped")
        return 1

    wanted: set[str] | None = None
    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}

    written: list[str] = []
    total_rows = 0

    # 対象 A: economy（CPI / IIP、月次）
    w, r = process_estat(cfg, app_id, raw_dir, log_dir, today_tag, wanted)
    written += w
    total_rows += r

    # 対象 B: population（人口推計 都道府県別 141 系列、年次）
    w, r = process_population(cfg, app_id, raw_dir, log_dir, today_tag, wanted)
    written += w
    total_rows += r

    if not written:
        logger.error("no series produced any rows")
        append_log(log_dir, "fetch_estat", "FAIL", "no series produced rows")
        return 1

    summary = f"series={len(written)} rows={total_rows}"
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_estat", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
