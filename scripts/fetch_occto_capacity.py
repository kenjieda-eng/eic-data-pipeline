#!/usr/bin/env python3
"""OCCTO 容量市場 メインオークション約定結果（Phase D 第 1 期、11 系列）

---------------------------------------------------------------------
ステータス: **Day 1 = fetcher scaffold (2026-05-20)**
  - Day 1 (5/20): 議事録 + scaffold (source_map.yaml occto-capacity 新設 +
                  scripts/fetch_occto_capacity.py 骨組み + processed/occto-capacity/ 作成)
  - Day 2 (5/21): 実 OCCTO サイト疎通 + PDF/Excel パース実装 + 5 年分 Backfill
                  → catalog 122 → 133 (容量市場 11 系列追加)
  - Day 3 (5/22): Insight #61 配置 (eic-data-web 側、リン MDX 原稿活用)
---------------------------------------------------------------------

設計議事録: docs/handover-2026-05-20.md (energy-data-platform 側)
案 A 採用根拠: 5/17 EDA さん発見「容量市場や需給調整市場のデータを取ってくるのはいつ頃？」
              → Phase D 第 1 期 (5/20-6/2) で容量 + 需給調整最優先実装
              → JEPX スポット既存と合わせて日本電力 3 大市場揃い達成

ターゲット 11 系列 (すべて年次、2020 年メインオークション開始〜最新年):
  価格 10: capacity-main-auction-price-{national,hokkaido,tohoku,tokyo,chubu,
           hokuriku,kansai,chugoku,shikoku,kyushu}
  容量 1:  capacity-main-auction-volume-total

【容量市場の基礎情報】
  - 運営: 電力広域的運営推進機関 (OCCTO)
  - 取引対象: 4 年後の供給力 (kW 価値、kW 年額)
  - 開催: 年 1 回 (毎年 7-9 月頃)
  - 第 1 回 = 2020 年実施 (FY2024 実需給対象)
  - 第 5 回 = 2024 年実施 (FY2028 実需給対象) [2026 年現在の最新]
  - 単位: 約定価格 ¥/kW・年、約定容量 kW

【Day 2 で確定予定の実装ポリシー】
  - OCCTO 公式サイトから約定結果 PDF/Excel を取得
  - PDF パース (pdfplumber) と Excel パース (openpyxl) の両対応
  - 年度別 (auction_year) ループで 11 系列 × 5 年分 = 55 行を Backfill
  - エリア別価格は全国一律の年は national にのみ計上、エリア別は null

Day 1 では以下のみ実装:
  1. load_source_cfg(): source_map.yaml から occto-capacity セクション読み込み
  2. list_auction_results(): スタブ (Day 2 で実装)
  3. download_auction_file(): スタブ (Day 2 で実装)
  4. parse_auction_excel(): スタブ (Day 2 で実装)
  5. main(): argparse + --dry-run + --backfill の骨組み (実 fetch は Day 2)
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

# 共通ライブラリ
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.common.http import get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_occto_capacity")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

SOURCE_KEY = "occto-capacity"
SOURCE_MAP_PATH = REPO_ROOT / "docs" / "source_map.yaml"
DATA_ROOT = REPO_ROOT / "data"
PROCESSED_DIR = DATA_ROOT / "processed" / "occto-capacity"
RAW_DIR = DATA_ROOT / "raw" / "occto-capacity"
LOG_DIR = DATA_ROOT / "_logs"

# 1 リクエストごとのスリープ (OCCTO サーバ配慮、2.0 秒)
SLEEP_BETWEEN_REQUESTS = 2.0

# エリアコード (エリア別価格抽出時の indicator_id サフィックス)
AREA_CODES = [
    "hokkaido", "tohoku", "tokyo", "chubu", "hokuriku",
    "kansai", "chugoku", "shikoku", "kyushu",
]

# 約定結果ファイル名パターン (Day 2 で実 URL 構造を確認後に精緻化)
# 暫定: "main_auction_YYYY" 形式を想定 (PDF or XLSX)
AUCTION_FILE_PATTERNS = [
    re.compile(r"main[_-]?auction[_-]?(\d{4})\.(pdf|xlsx?)$", re.IGNORECASE),
    re.compile(r"yakujo[_-]?(\d{4})\.(pdf|xlsx?)$", re.IGNORECASE),  # 約定 (yakujo)
]


# ---------------------------------------------------------------------------
# source_map.yaml 読み込み
# ---------------------------------------------------------------------------


def load_source_cfg() -> dict:
    """source_map.yaml から occto-capacity セクションを読み込む。

    Raises:
        KeyError: occto-capacity が未定義
    """
    with SOURCE_MAP_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sources = cfg.get("sources") or {}
    if SOURCE_KEY not in sources:
        raise KeyError(f"{SOURCE_MAP_PATH} に {SOURCE_KEY} セクションが見つかりません")
    return sources[SOURCE_KEY]


# ---------------------------------------------------------------------------
# OCCTO サイトから約定結果ファイルのリンク抽出
# ---------------------------------------------------------------------------


def list_auction_results(
    index_html: str,
    *,
    base_url: str,
    min_auction_year: int = 2020,
) -> list[dict]:
    """OCCTO index ページの HTML から約定結果ファイル (PDF/Excel) のリンクを抽出する。

    Day 1 = スタブ実装。Day 2 で実 HTML 構造を確認して精緻化予定。

    Args:
        index_html: index_url から取得した HTML 本文
        base_url: 相対 URL を絶対化するためのベース
        min_auction_year: このより古い年は除外 (default 2020 = 第 1 回オークション)

    Returns:
        list of dict: [
          {"auction_year": 2024,
           "delivery_fy": 2028,
           "url": "https://...main_auction_2024.xlsx",
           "filename": "main_auction_2024.xlsx",
           "filetype": "xlsx" or "pdf"},
          ...
        ]
        auction_year の昇順でソート。
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(index_html, "html.parser")
    candidates: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        lower = href.lower()
        if not (lower.endswith(".pdf") or lower.endswith(".xlsx") or lower.endswith(".xls")):
            continue
        abs_url = urljoin(base_url, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)

        filename = href.rsplit("/", 1)[-1]
        auction_year = _parse_auction_year_from_filename(filename)
        if auction_year is None:
            continue
        if auction_year < min_auction_year:
            continue

        # 実需給年度 = 約定年 + 4 (第 1 回 2020 → FY2024)
        delivery_fy = auction_year + 4
        filetype = "xlsx" if lower.endswith(".xlsx") else ("xls" if lower.endswith(".xls") else "pdf")

        candidates.append({
            "auction_year": auction_year,
            "delivery_fy": delivery_fy,
            "url": abs_url,
            "filename": filename,
            "filetype": filetype,
        })

    candidates.sort(key=lambda r: r["auction_year"])
    logger.info(
        "extracted %d auction result links (min_auction_year=%d)",
        len(candidates), min_auction_year,
    )
    return candidates


def _parse_auction_year_from_filename(filename: str) -> Optional[int]:
    """'main_auction_2024.xlsx' / 'yakujo_2024.pdf' から auction_year を返す。"""
    for pat in AUCTION_FILE_PATTERNS:
        m = pat.search(filename)
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# ファイルダウンロード
# ---------------------------------------------------------------------------


def download_auction_file(url: str, dest_path: Path) -> bytes:
    """約定結果ファイル (PDF/Excel) を download して raw ディレクトリに保存。

    既に dest_path が存在する場合は download せず、既存ファイルの bytes を返す。

    Day 1 = scaffold 実装。Day 2 で実 OCCTO サイト疎通テスト後に精緻化予定。
    """
    if dest_path.exists() and dest_path.stat().st_size > 1_000:
        logger.info("cache hit: %s (%d bytes)", dest_path, dest_path.stat().st_size)
        return dest_path.read_bytes()

    logger.info("GET: %s", url)
    r = get(url, timeout=60)
    r.raise_for_status()
    content = r.content
    if len(content) < 1_000:
        raise RuntimeError(
            f"downloaded file is suspiciously small ({len(content)} bytes); "
            f"content preview: {content[:200]!r}"
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(content)
    logger.info("saved raw: %s (%d bytes)", dest_path, len(content))
    return content


# ---------------------------------------------------------------------------
# Excel パース (Day 2 で本実装)
# ---------------------------------------------------------------------------


def parse_auction_excel(
    xlsx_bytes: bytes,
    cfg: dict,
    auction_year: int,
) -> list[dict]:
    """約定結果 Excel から 11 系列 × 1 年分の long 形式レコードを返す。

    Day 1 = スタブ実装 (空 list を返す)。Day 2 で実シート構造を確認して実装。

    予定する Day 2 の実装:
      - 第 1 シート (全国約定価格) から `capacity-main-auction-price-national` 抽出
      - 第 2 シート (エリア別価格) から `capacity-main-auction-price-{hokkaido..kyushu}` 抽出
      - 第 3 シート (約定容量) から `capacity-main-auction-volume-total` 抽出
      - エリア別が全国一律の年は national のみ計上、エリア値は null (rows に含めない)

    Args:
        xlsx_bytes: Excel ファイルの bytes
        cfg: source_map.yaml の occto-capacity セクション
        auction_year: オークション実施年 (e.g. 2024)
    Returns:
        list of {date, indicator_id, region, value} 形式
        Day 1 = 空 list (実装は Day 2)
    """
    logger.info(
        "parse_auction_excel(auction_year=%d): Day 1 scaffold (returns empty list, Day 2 で実装予定)",
        auction_year,
    )
    return []


def parse_auction_pdf(
    pdf_bytes: bytes,
    cfg: dict,
    auction_year: int,
) -> list[dict]:
    """約定結果 PDF から 11 系列 × 1 年分の long 形式レコードを返す。

    Day 1 = スタブ実装 (空 list を返す)。Day 2 で pdfplumber 実装予定。
    """
    logger.info(
        "parse_auction_pdf(auction_year=%d): Day 1 scaffold (returns empty list, Day 2 で実装予定)",
        auction_year,
    )
    return []


def _auction_year_to_date(auction_year: int) -> str:
    """auction_year を ISO date (YYYY-MM-DD) に変換。

    オークション実施日は毎年 7-9 月頃だが、本パイプラインでは年次系列として
    オークション年の 7 月 1 日を代表日付として採用する。
    """
    return f"{auction_year:04d}-07-01"


# ---------------------------------------------------------------------------
# メインパイプライン
# ---------------------------------------------------------------------------


def fetch_all_auctions(cfg: dict, *, backfill: bool, dry_run: bool) -> int:
    """公表済みオークションを走査、ファイルを download → parse → CSV 追記。

    Day 1 = scaffold 実装。Day 2 で実装ロジック追加予定。

    backfill=True:  min_auction_year (デフォルト 2020) 以降の全年度を走査
    backfill=False: 最新オークションのみ走査

    Returns:
        int: 新規に追記した行数 (全 indicator 合計)
    """
    index_url = cfg.get("index_url")
    if not index_url:
        logger.error("index_url が source_map.yaml に未設定")
        return 0

    min_year = int(cfg.get("min_auction_year") or 2020)

    if dry_run:
        logger.info(
            "--- dry-run: would fetch OCCTO main auction results from %s "
            "(min_auction_year=%d, backfill=%s) ---",
            index_url, min_year, backfill,
        )
        logger.info("Day 1 scaffold: real fetch + parse は Day 2 (5/21) で実装予定")
        return 0

    # --- 実行フェーズ (Day 2 で本実装) -----------------------------------------------
    logger.warning(
        "fetch_all_auctions: Day 1 scaffold は実 fetch を行わない。"
        "Day 2 (5/21) で list_auction_results + download + parse の実装が必要。"
    )

    processed_dir = PROCESSED_DIR
    raw_dir = RAW_DIR
    processed_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Day 2 で実装するロジックの骨組み (current 実装は no-op):
    # 1. fetch_index_page(index_url) で HTML 取得
    # 2. list_auction_results(html, base_url=index_url, min_auction_year=min_year)
    # 3. backfill=False なら最新 1 件のみに絞る
    # 4. 各 auction_year について:
    #    - download_auction_file(url, raw_path)
    #    - filetype に応じて parse_auction_excel or parse_auction_pdf
    #    - 結果を indicator_id ごとに accumulate
    # 5. accumulated を indicator_id 単位で write_processed + write_metadata_for_indicator

    accumulated: dict[str, list[dict]] = {}
    total_new_rows = 0

    for indicator_id, rows in sorted(accumulated.items()):
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["source_url"] = cfg.get("source_url", "")
        df = df[["date", "indicator_id", "region", "value", "source_url"]]
        try:
            write_processed(df, processed_dir, indicator_id)
            write_metadata_for_indicator(processed_dir, cfg, indicator_id, df)
            total_new_rows += len(df)
        except Exception as e:
            logger.error("write failed for %s: %s", indicator_id, e)

    return total_new_rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch OCCTO 容量市場 メインオークション約定結果 (Phase D 第 1 期)"
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="第 1 回 (2020 年) 以降の全オークションを再取得する",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="ダウンロードは行わず、設定読み込みのみテスト",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_source_cfg()
    except KeyError as e:
        logger.error(str(e))
        return 2

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    try:
        added = fetch_all_auctions(
            cfg,
            backfill=args.backfill,
            dry_run=args.dry_run,
        )
        logger.info("done: %d new rows appended", added)
        append_log(
            LOG_DIR, "fetch_occto_capacity", "ok",
            f"added={added} dry_run={args.dry_run} backfill={args.backfill}",
        )
        return 0
    except Exception as e:
        logger.exception("fetch failed: %s", e)
        append_log(LOG_DIR, "fetch_occto_capacity", "error", str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
