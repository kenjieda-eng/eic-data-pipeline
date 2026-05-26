"""
e-Stat API（政府統計の総合窓口）から日本マクロ 2 系列を取得するスクリプト。

対象（Phase 2 international batch 2、source_map.yaml の `estat` セクション）:
    - jpn-cpi-yoy              : 2020 年基準 消費者物価指数 総合 全国 前年同月比(%)
                                 statsDataId=0003427113 / tab=3 / cat01=0001 / area=00000
    - jpn-industrial-production : 鉱工業生産・出荷・在庫指数 2020 年基準 業種別原指数【月次】 鉱工業計
                                 statsDataId=0004015804 / cat01=0001000

方式:
    GET https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData
    params: appId, statsDataId, cdTab, cdCat01, cdArea, lang=J

認証:
    環境変数 `ESTAT_APP_ID` を python-dotenv 経由で .env から読み込む。
    無料登録（https://www.e-stat.go.jp/api/）で発行される 40 桁のキー。
    未設定時はワーキング系列なしでログのみ吐いて exit 1（FRED と異なり CSV フォールバックは無い）。

出力:
    - data/raw/estat/{indicator_id}_{YYYYMMDD}.json   (getStatsData 生レス)
    - data/processed/economy/{indicator_id}.csv       (共通スキーマ long 形式)
    - data/processed/economy/{indicator_id}.parquet
    - data/processed/economy/{indicator_id}.metadata.json (D-011)

ライセンス:
    e-Stat 利用規約。政府標準利用規約 2.0 互換、出典明示で再利用可。
    license_notice: "出典: 政府統計の総合窓口（e-Stat）。"

参考:
    - API 仕様: https://www.e-stat.go.jp/api/api-info/e-stat-manual3-0
    - 2026-05-27 実 API で統計表 ID と分類コードを確定（L-062 規律、ハードコード推測しない）。
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

# 「2026年4月」「2025年12月」など CPI 時間軸 name のパターン
CPI_TIME_RE = re.compile(r"^(\d{4})年(\d{1,2})月$")
# 「201801」「202502」など IIP 時間軸 name のパターン
IIP_TIME_RE = re.compile(r"^(\d{4})(\d{2})$")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Japan macro series from e-Stat API (CPI YoY / Industrial Production)"
    )
    parser.add_argument(
        "--series", type=str, default=None,
        help="カンマ区切りで indicator_id を絞る（例: jpn-cpi-yoy）",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="全期間取得（e-Stat 側が表に応じて全量返すため互換用フラグ）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    indicators_cfg: dict = source_cfg.get("indicators") or {}
    if not indicators_cfg:
        logger.error("source_map.yaml の %s.indicators が空です", SOURCE_KEY)
        return 2

    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}
        indicators_cfg = {k: v for k, v in indicators_cfg.items() if k in wanted}
        if not indicators_cfg:
            logger.error("--series で指定された ID が 1 つも該当しません: %s", args.series)
            return 2

    app_id = _get_app_id()
    raw_dir = ROOT / "data" / "raw" / "estat"
    processed_dir = ROOT / "data" / "processed" / "economy"
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

    api_base = source_cfg["api_base"]
    source_url = source_cfg.get("source_url", "")

    written: list[str] = []
    total_rows = 0

    for indicator_id, ind_cfg in indicators_cfg.items():
        stats_data_id = ind_cfg.get("stats_data_id")
        if not stats_data_id:
            logger.warning("%s: stats_data_id が未定義 — skip", indicator_id)
            continue

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
            "%s: %d rows (range=%s..%s, stats_data_id=%s)",
            indicator_id, len(long_df),
            long_df["date"].min(), long_df["date"].max(),
            stats_data_id,
        )

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
