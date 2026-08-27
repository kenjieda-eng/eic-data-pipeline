"""
財務省 国債金利情報（JGB yields, 日次）の取得スクリプト。

方式:
    GET https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv

    CSV 構造（Shift-JIS）:
        行 1:   単位注意書き（例: "（注）単位：％"）
        行 2:   列ヘッダ: 基準日,1年,2年,3年,4年,5年,6年,7年,8年,9年,10年,15年,20年,25年,30年,40年
        行 3+:  データ。1 行目カラム（基準日）は和暦日付（例 S49.9.24 / H10.4.1 / R6.4.1）。
                残り 15 列は新発国債の日次利回り（%）。新発がない年限は "-"。

対象:
    Phase 1 では 10 年新発国債のみ（jgb-10y-yield, finance domain）。
    将来 2年 / 5年 / 20年 / 30年 を追加する場合は source_map.yaml の
    mof-jgb.term_to_id に 1 行足すだけで対応可能な設計。

出力:
    - data/raw/jgb/jgbcm_all_{YYYYMMDD}.csv         （生ファイル）
    - data/processed/jgb/{indicator_id}.csv         （共通スキーマ long 形式）
    - data/processed/jgb/{indicator_id}.parquet
    - data/processed/jgb/{indicator_id}.metadata.json（D-011）

参考:
    財務省 金利情報: https://www.mof.go.jp/jgbs/reference/interest_rate/
    ライセンス: 財務省（国）の公表資料。原則として自由利用可。
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
logger = logging.getLogger("fetch_jgb")

SOURCE_KEY = "mof-jgb"

# 和暦 → 西暦 の元号オフセット（和暦 N 年 = 西暦 (OFFSET + N) 年）
# 例: S49 → 1925 + 49 = 1974, H10 → 1988 + 10 = 1998, R6 → 2018 + 6 = 2024
ERA_OFFSET = {
    "M": 1867,  # 明治 1 = 1868
    "T": 1911,  # 大正 1 = 1912
    "S": 1925,  # 昭和 1 = 1926
    "H": 1988,  # 平成 1 = 1989
    "R": 2018,  # 令和 1 = 2019
}

# 和暦日付のパーサ: [M|T|S|H|R] + 年 + "." + 月 + "." + 日
WAREKI_PATTERN = re.compile(r"^([MTSHR])(\d{1,2})\.(\d{1,2})\.(\d{1,2})$")


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_wareki(s: str) -> str | None:
    """
    和暦日付文字列（例 "S49.9.24", "H10.4.1", "R6.4.1"）を
    ISO 西暦（YYYY-MM-DD）に変換。不正値は None。
    """
    if not isinstance(s, str):
        return None
    text = s.strip()
    m = WAREKI_PATTERN.match(text)
    if not m:
        return None
    era, yy, mm, dd = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
    offset = ERA_OFFSET.get(era)
    if offset is None:
        return None
    year = offset + yy
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    try:
        return datetime(year, mm, dd).strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_yield(s: str) -> float | None:
    """'0.850' → 0.85。'-' や空文字、変換不能は None。"""
    if s is None:
        return None
    text = str(s).strip()
    if text in ("", "-", "--", "×"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_two_csvs(all_url: str, current_url: str | None) -> dict[str, bytes]:
    """
    アーカイブ版 (jgbcm_all.csv) と当年度版 (jgbcm.csv) を両方取得して dict で返す。
    current_url が None の場合はアーカイブだけ。当年度版が 404 などで取れない場合も
    アーカイブだけで継続する（後方互換、片方が落ちてもパイプラインは走る）。
    """
    out: dict[str, bytes] = {}
    # 必須: アーカイブ版
    logger.info("GET %s (archive)", all_url)
    r = get(all_url)
    r.raise_for_status()
    out["all"] = r.content

    # 任意: 当年度版（落ちてもアーカイブで動く）
    if current_url:
        try:
            logger.info("GET %s (current year)", current_url)
            r2 = get(current_url)
            r2.raise_for_status()
            out["current"] = r2.content
        except Exception as e:
            logger.warning("current_csv_url fetch failed (続行 with archive only): %s", e)
    return out


def fetch_csv(csv_url: str) -> bytes:
    """財務省サイトから CSV をそのまま取得してバイト列で返す。"""
    logger.info("GET %s", csv_url)
    r = get(csv_url)
    r.raise_for_status()
    return r.content


def decode_csv(raw_bytes: bytes, encoding: str = "shift_jis") -> str:
    """Shift-JIS バイト列を文字列に。decode 失敗は errors='replace' でフォールバック。"""
    try:
        return raw_bytes.decode(encoding)
    except UnicodeDecodeError:
        logger.warning("decode with %s failed — falling back to errors=replace", encoding)
        return raw_bytes.decode(encoding, errors="replace")


def parse_jgb_csv(text: str, header_row_idx: int = 1) -> pd.DataFrame:
    """
    財務省 CSV を DataFrame に。
    header_row_idx: 0-indexed の列ヘッダ行番号（2 行目なら 1）。
    戻り値: columns = ["基準日", "1年", "2年", ..., "40年"]、各セルは生テキスト。
    """
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) <= header_row_idx + 1:
        raise ValueError(f"jgbcm_all.csv に十分な行がありません（len={len(rows)}）")

    header = [h.strip() for h in rows[header_row_idx]]
    data_rows = rows[header_row_idx + 1:]
    # 空行と不正行を落とす
    clean: list[list[str]] = []
    for r in data_rows:
        if not r:
            continue
        # 行の長さを header に揃える（末尾カンマなど）
        if len(r) < len(header):
            r = r + [""] * (len(header) - len(r))
        elif len(r) > len(header):
            r = r[: len(header)]
        clean.append([c.strip() for c in r])

    df = pd.DataFrame(clean, columns=header)
    return df


def normalize_series(
    df_raw: pd.DataFrame,
    term_to_id: dict[str, str],
    source_url: str,
) -> dict[str, pd.DataFrame]:
    """
    wide 形式 DataFrame から、term_to_id に定義された年限だけを long 形式に抽出。
    戻り値: {indicator_id: DataFrame(共通スキーマ)} の dict。
    """
    if "基準日" not in df_raw.columns:
        raise ValueError(f"'基準日' 列が見つかりません。columns={list(df_raw.columns)}")

    # 日付列を西暦化
    iso_dates = df_raw["基準日"].map(parse_wareki)

    per_id: dict[str, pd.DataFrame] = {}
    for term, indicator_id in term_to_id.items():
        if term not in df_raw.columns:
            logger.warning("年限列 '%s' が CSV に存在しません — skip", term)
            continue
        values = df_raw[term].map(parse_yield)

        rows = []
        for d, v in zip(iso_dates, values):
            if d is None:
                continue
            if v is None:
                continue  # 欠損は書き出さない（missing_policy: null）
            rows.append({
                "date": d,
                "indicator_id": indicator_id,
                "region": "jp",
                "value": float(v),
                "source_url": source_url,
            })
        if not rows:
            logger.warning(
                "年限 '%s' → %s で 1 行もデータがありません（欠損のみ？）",
                term, indicator_id,
            )
            continue
        per_id[indicator_id] = pd.DataFrame(rows)

    return per_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch JGB yields CSV from MOF (Phase 1: 10y only by default)"
    )
    parser.add_argument(
        "--terms", type=str, default=None,
        help="カンマ区切りで年限を絞る（例: '10年,20年'）。省略時は source_map.yaml の term_to_id 全件",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    csv_url: str = source_cfg["csv_url"]
    encoding: str = source_cfg.get("encoding", "shift_jis")
    header_row_idx: int = int(source_cfg.get("header_row", 2)) - 1  # 1-indexed → 0-indexed
    term_to_id_cfg: dict[str, str] = source_cfg.get("term_to_id") or {}

    if not term_to_id_cfg:
        logger.error("source_map.yaml の %s.term_to_id が空です", SOURCE_KEY)
        return 2

    # --terms で絞る
    if args.terms:
        wanted = {t.strip() for t in args.terms.split(",") if t.strip()}
        term_to_id = {k: v for k, v in term_to_id_cfg.items() if k in wanted}
        if not term_to_id:
            logger.error("--terms で指定された年限が 1 つも該当しません: %s", args.terms)
            return 2
    else:
        term_to_id = dict(term_to_id_cfg)

    raw_dir = ROOT / "data" / "raw" / "jgb"
    processed_dir = ROOT / "data" / "processed" / "jgb"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    # ダウンロード（アーカイブ + 当年度版を両方）
    current_url = source_cfg.get("current_csv_url")  # source_map.yaml で追加した任意フィールド
    try:
        bundle = fetch_two_csvs(csv_url, current_url)
    except Exception as e:
        logger.exception("download failed: %s", e)
        append_log(log_dir, "fetch_jgb", "FAIL", f"download failed: {e}")
        return 1

    # 生ファイル保存（両方）
    save_raw(bundle["all"], raw_dir, f"jgbcm_all_{today_tag}.csv")
    if "current" in bundle:
        save_raw(bundle["current"], raw_dir, f"jgbcm_{today_tag}.csv")

    # デコード + パース（アーカイブ + 当年度版）
    try:
        text_all = decode_csv(bundle["all"], encoding=encoding)
        df_all = parse_jgb_csv(text_all, header_row_idx=header_row_idx)
        if "current" in bundle:
            text_cur = decode_csv(bundle["current"], encoding=encoding)
            df_cur = parse_jgb_csv(text_cur, header_row_idx=header_row_idx)
            # jgbcm.csv は末尾に空行とキャッシュ注意書き行が付くので、
            # 和暦パターンに合わない行をドロップしてから union する。
            wareki_re = r"^[MTSHR]\d"
            df_cur = df_cur[df_cur["基準日"].str.match(wareki_re, na=False)].reset_index(drop=True)
            # 当年度版が後勝ち（同じ基準日があればこちらで上書き）
            df_raw = pd.concat([df_all, df_cur], ignore_index=True)
            df_raw = df_raw.drop_duplicates(subset=["基準日"], keep="last").reset_index(drop=True)
            logger.info("merged: archive %d rows + current %d rows = %d rows after dedup",
                        len(df_all), len(df_cur), len(df_raw))
        else:
            df_raw = df_all
    except Exception as e:
        logger.exception("parse failed: %s", e)
        append_log(log_dir, "fetch_jgb", "FAIL", f"parse failed: {e}")
        return 1

    # 共通スキーマに正規化
    per_id = normalize_series(df_raw, term_to_id, source_url=csv_url)
    if not per_id:
        logger.error("どの年限も 0 行でした。CSV 構造が変わった可能性")
        append_log(log_dir, "fetch_jgb", "FAIL", "no series produced rows")
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
    # （updated_at = 生存信号）。JGB は 1 CSV に全年限が入るため、ここに到達した時点で
    # term_to_id の全年限がフェッチ成功範囲（失敗時は上で return 1）。
    expected_ids = set(term_to_id.values())
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
    append_log(log_dir, "fetch_jgb", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
