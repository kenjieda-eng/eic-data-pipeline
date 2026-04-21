"""
気象庁（JMA）日次気温の取得スクリプト。

方式:
    GET https://www.data.jma.go.jp/obd/stats/etrn/view/daily_s1.php
        ?prec_no={prec_no}&block_no={block_no}&year={year}&month={month}&day=&view=
    セッションやトークン不要の静的 HTML。Shift_JIS エンコード。
    テーブル class="data2_s" に日次観測値が入っている。

対象:
    9 エリアの代表観測点（気象官署）× 3 項目（平均気温／最高気温／最低気温）
    = 27 系列

出力:
    - data/raw/jma/daily_s1_{area}_{year}_{month:02d}.html   （生 HTML）
    - data/processed/jma/jma-temp-{avg|max|min}-{area}.csv   （共通スキーマ long 形式）
    - data/processed/jma/jma-temp-{avg|max|min}-{area}.parquet
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml
from bs4 import BeautifulSoup

# 連続取得時、JMA サーバに配慮して挟むスリープ秒数。
# 月単位で回すため、9 地点 × 180 ヶ月（15 年）= 1,620 リクエストで一番効く。
# 1.5 秒 × 1,620 = 約 40 分。バックフィル時のみ効く値。
SLEEP_BETWEEN_REQUESTS = 1.5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.http import make_session, session_get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_jma")

SOURCE_KEY = "jma-temp"

# JMA の欠測・特殊記号。これらが含まれるセルは NaN に変換する。
MISSING_MARKERS = ("///", "--", "×", ")", "*", "#", "")


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_numeric(text: str) -> float | None:
    """JMA セルのテキストを float に変換。欠測は None。"""
    if text is None:
        return None
    s = text.strip().replace("\xa0", "").replace(" ", "")
    # 末尾の ")" は「利用に際しては注意が必要」の記号。数値部分は使う。
    s = s.rstrip(")").rstrip("]").rstrip("*").rstrip("#")
    if s in MISSING_MARKERS or s == "":
        return None
    # "///" が含まれるケース
    if "/" in s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def extract_daily_rows(
    html: str,
    table_class: str,
    col_idx: dict[str, int],
    year: int,
    month: int,
) -> list[dict]:
    """
    HTML から該当テーブルを探し、日次行を抽出する。
    返値: [{"date": "2024-01-01", "temp_avg": ..., "temp_max": ..., "temp_min": ...}, ...]
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=table_class)
    if table is None:
        return []

    rows_out: list[dict] = []
    trs = table.find_all("tr")
    # ヘッダ行をスキップ: "日" というセルを持つ最初の行より後をデータ行とする。
    # 実際の daily_s1 は 2 段ヘッダ + データ行が 28-31 行続く構造。
    data_started = False
    for tr in trs:
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        first_text = tds[0].get_text(strip=True)
        # データ行の先頭セルは「日」の数字（1〜31）。
        if not first_text.isdigit():
            continue
        day = int(first_text)
        if day < 1 or day > 31:
            continue
        data_started = True
        # 列数が足りないラインは壊れている可能性（スキップ）
        max_idx = max(col_idx.values())
        if len(tds) <= max_idx:
            continue
        try:
            d = datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            # 2月30日など存在しない日（テーブルには原則出ないが念のため）
            continue
        row = {"date": d, "day": day}
        for key, idx in col_idx.items():
            if key == "day":
                continue
            cell_text = tds[idx].get_text(strip=True)
            row[key] = parse_numeric(cell_text)
        rows_out.append(row)

    if not data_started:
        logger.warning("no data rows found in table (year=%d month=%d)", year, month)
    return rows_out


def fetch_month(
    session,
    url_template: str,
    encoding: str,
    table_class: str,
    col_idx: dict[str, int],
    area: str,
    prec_no: int,
    block_no: int,
    year: int,
    month: int,
    raw_dir: Path,
) -> list[dict]:
    """1 地点 × 1 ヶ月分を取得してパースする。"""
    url = url_template.format(
        prec_no=prec_no, block_no=block_no, year=year, month=month,
    )
    logger.info("GET area=%s year=%d month=%02d", area, year, month)
    r = session_get(session, url)
    if r.status_code >= 400:
        logger.warning(
            "HTTP %d area=%s year=%d month=%02d — skipping",
            r.status_code, area, year, month,
        )
        return []

    # Shift_JIS 想定（サーバがヘッダで言ってこない場合もあるので明示指定）
    r.encoding = encoding or r.apparent_encoding or "shift_jis"
    html = r.text

    # 生 HTML を保存（デバッグ + 監査用）
    fname = f"daily_s1_{area}_{year}_{month:02d}.html"
    save_raw(r.content, raw_dir, fname)

    rows = extract_daily_rows(html, table_class, col_idx, year, month)
    logger.info(
        "parsed area=%s year=%d month=%02d rows=%d",
        area, year, month, len(rows),
    )
    return rows


def normalize_rows(
    rows: list[dict],
    area: str,
    items: list[dict],
    source_url: str,
) -> pd.DataFrame:
    """日次生値を共通スキーマの long 形式に変換。"""
    if not rows:
        return pd.DataFrame(columns=["date", "indicator_id", "region", "value", "source_url"])
    out: list[dict] = []
    for row in rows:
        date = row["date"]
        for item in items:
            key = item["key"]
            if key not in row:
                continue
            value = row[key]
            if value is None:
                continue  # 欠測は書かない（後で null/gap として扱う）
            indicator_id = f"{item['id_prefix']}-{area}"
            out.append({
                "date": date,
                "indicator_id": indicator_id,
                "region": area,
                "value": float(value),
                "source_url": source_url,
            })
    return pd.DataFrame(out)


def month_iter(start: tuple[int, int], end: tuple[int, int]) -> Iterable[tuple[int, int]]:
    """(year, month) の包括範囲を順に返す。"""
    y, m = start
    ey, em = end
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def pick_months(args, now_jst: datetime) -> list[tuple[int, int]]:
    """取得対象の (year, month) リストを確定する。"""
    cur_y, cur_m = now_jst.year, now_jst.month

    if args.month:
        y, m = map(int, args.month.split("-"))
        return [(y, m)]

    if args.all:
        return list(month_iter((2012, 1), (cur_y, cur_m)))

    if args.since:
        y, m = map(int, args.since.split("-"))
        return list(month_iter((y, m), (cur_y, cur_m)))

    # default: 直近 2 ヶ月（当月 + 前月）。月初の境界を確実に拾うため。
    prev_y, prev_m = (cur_y, cur_m - 1) if cur_m > 1 else (cur_y - 1, 12)
    return [(prev_y, prev_m), (cur_y, cur_m)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch JMA daily temperature (9 stations, 3 items)")
    parser.add_argument("--all", action="store_true", help="2012-01 から現在まで全期間を取得")
    parser.add_argument("--since", type=str, default=None, help="YYYY-MM から現在まで（例: 2020-01）")
    parser.add_argument("--month", type=str, default=None, help="単月のみ取得（例: 2024-01）")
    parser.add_argument("--areas", type=str, default=None,
                        help="カンマ区切りで地点を絞る（例: tokyo,osaka）。省略時は全 9 エリア")
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    page_url_template: str = source_cfg["page_url_template"]
    encoding: str = source_cfg.get("encoding", "shift_jis")
    table_class: str = source_cfg.get("table_class", "data2_s")
    col_idx: dict[str, int] = source_cfg["columns"]
    stations: dict[str, dict] = source_cfg["stations"]
    items: list[dict] = source_cfg["items"]

    # --areas で絞る
    if args.areas:
        wanted = [a.strip() for a in args.areas.split(",") if a.strip()]
        stations = {k: v for k, v in stations.items() if k in wanted}
        if not stations:
            logger.error("--areas で指定された地点が 1 つも該当しません: %s", args.areas)
            return 2

    raw_dir = ROOT / "data" / "raw" / "jma"
    processed_dir = ROOT / "data" / "processed" / "jma"
    log_dir = ROOT / "data" / "_logs"

    # JST で「今日」を確定
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    target_months = pick_months(args, now_jst)
    logger.info(
        "target: areas=%s months=%d (first=%s last=%s)",
        list(stations.keys()),
        len(target_months),
        f"{target_months[0][0]}-{target_months[0][1]:02d}" if target_months else "-",
        f"{target_months[-1][0]}-{target_months[-1][1]:02d}" if target_months else "-",
    )

    session = make_session()

    total_requests = len(stations) * len(target_months)
    logger.info("total requests planned: %d (≒ %.1f min at %.1fs sleep)",
                total_requests,
                total_requests * SLEEP_BETWEEN_REQUESTS / 60,
                SLEEP_BETWEEN_REQUESTS)

    # 地点ごとに long 形式の rows を蓄える
    per_area_rows: dict[str, list[dict]] = {area: [] for area in stations.keys()}
    failed: list[tuple[str, int, int]] = []

    req_idx = 0
    for area, st in stations.items():
        for (year, month) in target_months:
            if req_idx > 0 and SLEEP_BETWEEN_REQUESTS > 0:
                time.sleep(SLEEP_BETWEEN_REQUESTS)
            req_idx += 1
            try:
                rows = fetch_month(
                    session=session,
                    url_template=page_url_template,
                    encoding=encoding,
                    table_class=table_class,
                    col_idx=col_idx,
                    area=area,
                    prec_no=st["prec_no"],
                    block_no=st["block_no"],
                    year=year,
                    month=month,
                    raw_dir=raw_dir,
                )
            except Exception as e:
                logger.exception("fetch failed area=%s year=%d month=%d: %s",
                                 area, year, month, e)
                failed.append((area, year, month))
                continue
            if not rows:
                failed.append((area, year, month))
                continue
            per_area_rows[area].extend(rows)

    # 地点 × 項目ごとに processed CSV を書く
    written_files: list[str] = []
    total_rows = 0
    for area, rows in per_area_rows.items():
        if not rows:
            logger.warning("area=%s: no rows gathered", area)
            continue
        source_url = page_url_template.format(
            prec_no=stations[area]["prec_no"],
            block_no=stations[area]["block_no"],
            year="YYYY", month="MM",
        )
        df = normalize_rows(rows, area, items, source_url)
        if df.empty:
            continue
        # indicator_id ごとに分割して書き出し（共通スキーマ、既存とマージ）
        for indicator_id, group in df.groupby("indicator_id"):
            write_processed(group, processed_dir, basename=str(indicator_id))
            written_files.append(str(indicator_id))
            total_rows += len(group)

    summary = (
        f"months={len(target_months)} areas={len(stations)} "
        f"rows={total_rows} files={len(written_files)} "
        f"failed={len(failed)}"
    )
    logger.info("done: %s", summary)

    if total_rows == 0:
        logger.error("no rows written — likely URL/parse issue")
        append_log(log_dir, "fetch_jma", "FAIL", summary)
        return 1

    append_log(log_dir, "fetch_jma", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
