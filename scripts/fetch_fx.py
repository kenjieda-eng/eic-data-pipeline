"""
為替レート（BOJ FM08）の取得スクリプト（月次）。

方式:
    GET https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=csv&lang=en&db=FM08
        &code=FXERM07,FXERM06,FXERM03,FXERM05&startDate=197301&endDate=203103

    CSV レスポンス構造:
        行 1-11: STATUS, MESSAGEID, MESSAGE, DATE, PARAMETER×5, STARTPOSITION, NEXTPOSITION
        行 12:   列ヘッダ "SERIES_CODE,NAME_OF_TIME_SERIES,UNIT,FREQUENCY,CATEGORY,LAST_UPDATE,SURVEY_DATES,VALUES"
        行 13+:  データ（1 行 = 1 系列の 1 月）

対象:
    4 系列（USD/JPY: 月中平均, 月末値, 月内高値, 月内安値）
    将来、日次系列（FXERD04 など）を別スクリプトで追加予定。

出力:
    - data/raw/fx/boj_FM08_{YYYYMMDD}.csv         （生ファイル）
    - data/processed/fx/{indicator_id}.csv        （共通スキーマ long 形式）
    - data/processed/fx/{indicator_id}.parquet

参考:
    BOJ 時系列統計データ検索サイト: https://www.stat-search.boj.or.jp/
    API マニュアル: https://www.stat-search.boj.or.jp/info/api_manual_en.pdf
    ライセンス: 利用規約に従い、以下の文言を明示する必要あり。
      「このサービスは、日本銀行時系列統計データ検索サイトの API 機能を使用しています。
       サービスの内容は日本銀行によって保証されたものではありません。」
"""

from __future__ import annotations

import argparse
import csv
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_fx")

SOURCE_KEY = "boj-fx"

# "202603" 形式（YYYYMM）をパースするための正規表現
YYYYMM_PATTERN = re.compile(r"^(\d{4})(\d{2})$")


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_yyyymm(code: str) -> str | None:
    """'202603' → '2026-03-01' のように月初日に正規化。不正値は None。"""
    if not isinstance(code, str):
        return None
    m = YYYYMM_PATTERN.match(code.strip())
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2))
    if not (1 <= month <= 12):
        return None
    try:
        return datetime(year, month, 1).strftime("%Y-%m-%d")
    except ValueError:
        return None


def build_end_date(buffer_years: int) -> str:
    """現在年月から buffer_years 年先の YYYYMM を返す（API 側でクランプされる）。"""
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    end_year = now_jst.year + buffer_years
    return f"{end_year:04d}{now_jst.month:02d}"


def fetch_csv(api_url: str, db: str, codes: list[str], start_date: str, end_date: str) -> bytes:
    """BOJ API から CSV を 1 発で取得してバイト列で返す。"""
    params = {
        "format": "csv",
        "lang": "en",
        "db": db,
        "code": ",".join(codes),
        "startDate": start_date,
        "endDate": end_date,
    }
    logger.info("GET %s params=%s", api_url, params)
    r = get(api_url, params=params, timeout=60)
    r.raise_for_status()
    if len(r.content) < 1_000:
        raise RuntimeError(
            f"downloaded csv is suspiciously small ({len(r.content)} bytes); "
            f"preview={r.content[:400]!r}"
        )
    logger.info("downloaded %d bytes", len(r.content))
    return r.content


def parse_boj_csv(csv_bytes: bytes) -> pd.DataFrame:
    """
    BOJ の getDataCode CSV をパースして DataFrame にする。

    先頭 11 行は STATUS/PARAMETER/NEXTPOSITION のメタデータヘッダ。
    12 行目が列ヘッダ:
      "SERIES_CODE,NAME_OF_TIME_SERIES,UNIT,FREQUENCY,CATEGORY,LAST_UPDATE,SURVEY_DATES,VALUES"
    13 行目以降が実データ。

    堅牢性のため、メタデータ行数が将来変わる可能性を考慮して
    "SERIES_CODE" で始まる行を列ヘッダとして動的に検出する。
    """
    text = csv_bytes.decode("utf-8")
    lines = text.splitlines()

    # まず STATUS を確認（先頭行は "STATUS,200" 形式）
    status_ok = False
    for line in lines[:5]:
        if line.startswith("STATUS,"):
            parts = line.split(",", 1)
            if len(parts) == 2 and parts[1].strip() == "200":
                status_ok = True
            break
    if not status_ok:
        raise RuntimeError(f"BOJ API returned non-200 STATUS. Preview: {lines[:8]!r}")

    # 列ヘッダ行を探す
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("SERIES_CODE,"):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("could not find SERIES_CODE header row in BOJ CSV")

    # csv モジュールで残りをパース（VALUES 行にカンマ含む NAME があるためクォート処理必須）
    reader = csv.reader(io.StringIO("\n".join(lines[header_idx:])))
    rows = list(reader)
    if not rows:
        raise RuntimeError("empty data section")

    header = rows[0]
    records = rows[1:]
    df = pd.DataFrame(records, columns=header)

    # 必要な列が揃っているか検証
    required = {"SERIES_CODE", "SURVEY_DATES", "VALUES"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"missing required columns in CSV: {missing}. Got {list(df.columns)}")

    logger.info("parsed %d data rows from BOJ CSV (header at line %d)", len(df), header_idx + 1)
    return df


def normalize_series(
    df_raw: pd.DataFrame,
    series_defs: list[dict],
    source_url: str,
) -> dict[str, pd.DataFrame]:
    """
    series_defs の各エントリについて、SERIES_CODE でフィルタし long 形式に正規化する。

    series_defs の各エントリ:
        { id: 'fx-usdjpy-monthly-avg', code: 'FXERM07', region: 'jp', ... }
    """
    out: dict[str, pd.DataFrame] = {}

    for sd in series_defs:
        indicator_id = sd["id"]
        code = sd["code"]
        region = sd.get("region", "jp")

        sub = df_raw[df_raw["SERIES_CODE"].astype(str).str.strip() == code]
        if sub.empty:
            logger.warning(
                "series=%s: no rows matched SERIES_CODE=%s — skip",
                indicator_id, code,
            )
            continue

        rows = []
        for _, rr in sub.iterrows():
            ymd = parse_yyyymm(str(rr["SURVEY_DATES"]))
            if ymd is None:
                continue
            raw = rr["VALUES"]
            raw_str = str(raw).strip()
            # BOJ は欠測を空文字 or "ND" で表すケースがある
            if raw_str == "" or raw_str.upper() == "ND":
                continue
            try:
                value = float(raw_str)
            except (TypeError, ValueError):
                logger.debug("skip non-numeric VALUE for %s at %s: %r", indicator_id, ymd, raw)
                continue
            rows.append({
                "date": ymd,
                "indicator_id": indicator_id,
                "region": region,
                "value": value,
                "source_url": source_url,
            })
        if not rows:
            logger.warning("series=%s: 0 rows after normalize — skip", indicator_id)
            continue
        df_out = pd.DataFrame(rows)
        out[indicator_id] = df_out
        logger.info(
            "series=%s (%s): %d rows (range=%s..%s)",
            indicator_id, code, len(df_out),
            df_out["date"].min(), df_out["date"].max(),
        )

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch BOJ FM08 monthly USD/JPY FX rates")
    parser.add_argument(
        "--series",
        type=str,
        default=None,
        help="カンマ区切りで indicator_id を絞る（例: fx-usdjpy-monthly-avg）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    api_url = source_cfg["api_url"]
    db = source_cfg["db"]
    start_date = source_cfg["start_date"]
    end_date_buffer_years = int(source_cfg.get("end_date_buffer_years", 3))
    end_date = build_end_date(end_date_buffer_years)
    series_defs: list[dict] = source_cfg["series"]

    # --series で絞り込み
    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}
        series_defs = [s for s in series_defs if s["id"] in wanted]
        if not series_defs:
            logger.error("--series で指定された ID が 1 つも該当しません: %s", args.series)
            return 2

    raw_dir = ROOT / "data" / "raw" / "fx"
    processed_dir = ROOT / "data" / "processed" / "fx"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    # API 側に渡す code 一覧（BOJ 側のシリーズコード）
    codes = [sd["code"] for sd in series_defs]
    if not codes:
        logger.error("no codes to fetch")
        return 2

    # ダウンロード
    try:
        csv_bytes = fetch_csv(api_url, db, codes, start_date, end_date)
    except Exception as e:
        logger.exception("download failed: %s", e)
        append_log(log_dir, "fetch_fx", "FAIL", f"download failed: {e}")
        return 1

    # 生ファイル保存
    save_raw(csv_bytes, raw_dir, f"boj_{db}_{today_tag}.csv")

    # パース
    try:
        df_raw = parse_boj_csv(csv_bytes)
    except Exception as e:
        logger.exception("parse failed: %s", e)
        append_log(log_dir, "fetch_fx", "FAIL", f"parse failed: {e}")
        return 1

    # 共通スキーマに正規化
    source_url = f"{api_url}?db={db}&code={','.join(codes)}"
    per_id = normalize_series(df_raw, series_defs, source_url=source_url)
    if not per_id:
        logger.error("no series produced any rows — likely series code change at BOJ")
        append_log(log_dir, "fetch_fx", "FAIL", "no series produced rows")
        return 1

    # 書き出し
    written: list[str] = []
    total_rows = 0
    for indicator_id, df in per_id.items():
        write_processed(df, processed_dir, basename=indicator_id)
        # D-011: 系列メタデータを {id}.metadata.json に書き出す
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, df)
        written.append(indicator_id)
        total_rows += len(df)

    # D-020④: フェッチ成功範囲で行が来なかった indicator も metadata を書き直す
    # （updated_at = 生存信号）。BOJ API は全 code を 1 リクエストで取得するため、
    # ここに到達した時点で series_defs 全系列がフェッチ成功範囲（失敗時は上で return 1）。
    expected_ids = {sd["id"] for sd in series_defs}
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
    append_log(log_dir, "fetch_fx", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
