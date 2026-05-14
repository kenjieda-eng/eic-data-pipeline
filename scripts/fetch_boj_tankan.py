"""
日銀短観 業況判断 DI（BOJ db=CO）の取得スクリプト（四半期）。

方式:
    GET https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=csv&lang=en&db=CO
        &code=TK99F1000601GCQ01000,TK99F2000601GCQ01000,...

    CSV レスポンス構造は FM08（boj-fx）と同一:
        行 1-11: STATUS, MESSAGEID, MESSAGE, DATE, PARAMETER×5, STARTPOSITION, NEXTPOSITION
        行 12:   列ヘッダ "SERIES_CODE,NAME_OF_TIME_SERIES,UNIT,FREQUENCY,CATEGORY,LAST_UPDATE,SURVEY_DATES,VALUES"
        行 13+:  データ（SURVEY_DATES は四半期 "YYYYNN"、NN=01..04）

対象（4 系列）:
    - tankan-large-mfg-di    : TK99F1000601GCQ01000（大企業・製造業）
    - tankan-large-nonmfg-di : TK99F2000601GCQ01000（大企業・非製造業）
    - tankan-sme-mfg-di      : TK99F1000601GCQ03000（中小企業・製造業）
    - tankan-sme-nonmfg-di   : TK99F2000601GCQ03000（中小企業・非製造業）

出力:
    - data/raw/boj-tankan/boj_CO_{YYYYMMDD}.csv
    - data/processed/tankan/{indicator_id}.csv
    - data/processed/tankan/{indicator_id}.parquet
    - data/processed/tankan/{indicator_id}.metadata.json

参考:
    系列コード仕様: https://www.stat-search.boj.or.jp/info/tankan_code_en.html
    API マニュアル: https://www.stat-search.boj.or.jp/info/api_manual_en.pdf
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
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_boj_tankan")

SOURCE_KEY = "boj-tankan"

# Tankan の SURVEY_DATES は "YYYYNN"（NN=01..04）。Q1=Mar, Q2=Jun, Q3=Sep, Q4=Dec に正規化。
YYYYQN_PATTERN = re.compile(r"^(\d{4})(0[1-4])$")
QUARTER_TO_MONTH = {1: 3, 2: 6, 3: 9, 4: 12}


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_yyyyqn(code: str) -> str | None:
    """'197402' → '1974-06-01'（Q2 = June）のように survey-month で月初日に正規化。不正値は None。"""
    if not isinstance(code, str):
        return None
    m = YYYYQN_PATTERN.match(code.strip())
    if not m:
        return None
    year = int(m.group(1))
    quarter = int(m.group(2))
    month = QUARTER_TO_MONTH.get(quarter)
    if month is None:
        return None
    try:
        return datetime(year, month, 1).strftime("%Y-%m-%d")
    except ValueError:
        return None


def fetch_csv(api_url: str, db: str, codes: list[str]) -> bytes:
    """BOJ API から CSV を 1 発で取得してバイト列で返す。

    四半期データは startDate/endDate のパラメータ仕様が確認できなかったため、
    両者を省略して全期間を取得する。"""
    params = {
        "format": "csv",
        "lang": "en",
        "db": db,
        "code": ",".join(codes),
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
    BOJ の getDataCode CSV をパースして DataFrame にする。boj-fx と同構造。

    先頭 11 行は STATUS/PARAMETER/NEXTPOSITION のメタデータヘッダ。
    12 行目が列ヘッダ:
      "SERIES_CODE,NAME_OF_TIME_SERIES,UNIT,FREQUENCY,CATEGORY,LAST_UPDATE,SURVEY_DATES,VALUES"
    13 行目以降が実データ。

    堅牢性のため、"SERIES_CODE" で始まる行を動的に検出。
    """
    text = csv_bytes.decode("utf-8")
    lines = text.splitlines()

    status_ok = False
    for line in lines[:5]:
        if line.startswith("STATUS,"):
            parts = line.split(",", 1)
            if len(parts) == 2 and parts[1].strip() == "200":
                status_ok = True
            break
    if not status_ok:
        raise RuntimeError(f"BOJ API returned non-200 STATUS. Preview: {lines[:8]!r}")

    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("SERIES_CODE,"):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("could not find SERIES_CODE header row in BOJ CSV")

    reader = csv.reader(io.StringIO("\n".join(lines[header_idx:])))
    rows = list(reader)
    if not rows:
        raise RuntimeError("empty data section")

    header = rows[0]
    records = rows[1:]
    df = pd.DataFrame(records, columns=header)

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
    """series_defs の各エントリについて、SERIES_CODE でフィルタし long 形式に正規化する。"""
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
            ymd = parse_yyyyqn(str(rr["SURVEY_DATES"]))
            if ymd is None:
                continue
            raw = rr["VALUES"]
            raw_str = str(raw).strip()
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
    parser = argparse.ArgumentParser(description="Fetch BOJ Tankan quarterly Business Conditions DI")
    parser.add_argument(
        "--series",
        type=str,
        default=None,
        help="カンマ区切りで indicator_id を絞る（例: tankan-large-mfg-di）",
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
    series_defs: list[dict] = source_cfg["series"]

    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}
        series_defs = [s for s in series_defs if s["id"] in wanted]
        if not series_defs:
            logger.error("--series で指定された ID が 1 つも該当しません: %s", args.series)
            return 2

    raw_dir = ROOT / "data" / "raw" / "boj-tankan"
    processed_dir = ROOT / "data" / "processed" / "tankan"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    codes = [sd["code"] for sd in series_defs]
    if not codes:
        logger.error("no codes to fetch")
        return 2

    try:
        csv_bytes = fetch_csv(api_url, db, codes)
    except Exception as e:
        logger.exception("download failed: %s", e)
        append_log(log_dir, "fetch_boj_tankan", "FAIL", f"download failed: {e}")
        return 1

    save_raw(csv_bytes, raw_dir, f"boj_{db}_{today_tag}.csv")

    try:
        df_raw = parse_boj_csv(csv_bytes)
    except Exception as e:
        logger.exception("parse failed: %s", e)
        append_log(log_dir, "fetch_boj_tankan", "FAIL", f"parse failed: {e}")
        return 1

    source_url = f"{api_url}?db={db}&code={','.join(codes)}"
    per_id = normalize_series(df_raw, series_defs, source_url=source_url)
    if not per_id:
        logger.error("no series produced any rows — likely series code change at BOJ")
        append_log(log_dir, "fetch_boj_tankan", "FAIL", "no series produced rows")
        return 1

    written: list[str] = []
    total_rows = 0
    for indicator_id, df in per_id.items():
        write_processed(df, processed_dir, basename=indicator_id)
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, df)
        written.append(indicator_id)
        total_rows += len(df)

    summary = f"series={len(written)} rows={total_rows}"
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_boj_tankan", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
