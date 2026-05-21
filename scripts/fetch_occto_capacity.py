#!/usr/bin/env python3
"""OCCTO 容量市場 メインオークション約定結果 (Phase D 第 1 期、11 系列)

---------------------------------------------------------------------
ステータス: **Day 2 (2026-05-20 午後) = 全面書き直し版**
  - Day 1 (5/20 朝): scaffold 配置 → 5/20 午後の probe で 6 件ドリフト発覚
  - Day 2 (5/20 午後): probe (FY2024 + FY2028 ZIP) で実構造完全把握 + 本ファイル全面書き直し
  - Day 3 (5/21 朝): Backfill 実行 (FY2024-FY2029 メイン 6 回分 + 11 系列の CSV/metadata 配置)
                     → catalog 122 → 133 達成予定
---------------------------------------------------------------------

設計議事録: docs/handover-2026-05-20.md (energy-data-platform 側)
ドリフト記録: L-061 候補 (scaffold 起草時の実 URL HTTP リーチ確認不足、5/20 体系化予定)

【容量市場の基礎情報】
  - 運営: 電力広域的運営推進機関 (OCCTO)
  - 取引対象: 4 年後の供給力 (kW 価値、kW 年額)
  - 開催: 年 1 回 (毎年 7-9 月頃)
  - 第 1 回 = 2020 年実施 (FY2024 実需給対象)
  - 第 6 回 = 2025 年実施 (FY2029 実需給対象) [2026 年現在の最新]
  - 単位: 約定価格 ¥/kW・年、約定容量 kW

【データ取得構造 (Day 2 probe で実体把握)】
  - index ページ: https://www.occto.or.jp/capacity-market/yoryoshijyo/main/data/
  - 約定結果 ZIP: {fy}_yakujoukekka_csv.zip (FY2028 のみ {fy}_main_yakujoukekka_csv.zip)
  - ZIP 内 = 1 枚の CSV (cp932 エンコード、5 列、9 エリア行)
  - 列構造: 対象実需給年度 / エリア / 約定価格[円/kW] / エリア毎の約定容量[kW] / エリア毎の約定総額[円]
  - エリア順: 北海道, 東北, 東京, 中部, 北陸, 関西, 中国, 四国, 九州

【11 系列構成】
  価格 10: capacity-main-auction-price-{national, hokkaido, tohoku, tokyo, chubu,
           hokuriku, kansai, chugoku, shikoku, kyushu}
    - 9 エリア = CSV 直読み (¥/kW)
    - national = Σ(エリア価格 × エリア容量) / Σ(エリア容量) 加重平均
  容量 1:  capacity-main-auction-volume-total = Σ(9 エリア容量) (kW)

【date 軸】
  - 実需給年度の 4 月 1 日 (FY2024 → 2024-04-01)
  - オークション実施日 (2020 年 7 月等) とは別、データ点の表現として実需給開始日を採用
"""
from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

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

# 1 リクエストごとのスリープ (OCCTO サーバ配慮)
SLEEP_BETWEEN_REQUESTS = 2.0

# 約定結果 ZIP ファイル名パターン (Day 2 probe で実体把握)
# FY2024-FY2029 を網羅。FY2028 のみ "main" が中央に入る変則。
# capture group 1 = delivery_fy (4 桁の年)
AUCTION_ZIP_PATTERNS = [
    re.compile(r"(\d{4})_yakujoukekka_csv\.zip$", re.IGNORECASE),
    re.compile(r"(\d{4})_main_yakujoukekka_csv\.zip$", re.IGNORECASE),
]

# 追加オークションは別 indicator (現スコープ外) のため除外パターン
EXCLUDE_PATTERNS = [
    re.compile(r"_tsuika_", re.IGNORECASE),  # 追加オークション
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
# OCCTO index ページから ZIP リンク抽出
# ---------------------------------------------------------------------------


def _parse_delivery_fy_from_filename(filename: str) -> Optional[int]:
    """'2024_yakujoukekka_csv.zip' / '2028_main_yakujoukekka_csv.zip' から
    delivery_fy (実需給年度、4 桁西暦) を返す。
    """
    for pat in EXCLUDE_PATTERNS:
        if pat.search(filename):
            return None
    for pat in AUCTION_ZIP_PATTERNS:
        m = pat.search(filename)
        if m:
            return int(m.group(1))
    return None


def list_auction_zips(
    index_html: str,
    *,
    base_url: str,
    min_delivery_fy: int = 2024,
) -> list[dict]:
    """OCCTO index ページの HTML から約定結果 ZIP のリンクを抽出する。

    Args:
        index_html: index_url から取得した HTML 本文
        base_url: 相対 URL を絶対化するためのベース
        min_delivery_fy: このより古い年度は除外 (default 2024 = 第 1 回オークション分)

    Returns:
        list of dict: [
          {"delivery_fy": 2024,
           "url": "https://...2024_yakujoukekka_csv.zip",
           "filename": "2024_yakujoukekka_csv.zip"},
          ...
        ]
        delivery_fy の昇順でソート。重複は dedup。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(index_html, "html.parser")
    candidates: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if not href.lower().endswith(".zip"):
            continue
        abs_url = urljoin(base_url, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)

        filename = href.rsplit("/", 1)[-1]
        delivery_fy = _parse_delivery_fy_from_filename(filename)
        if delivery_fy is None:
            continue
        if delivery_fy < min_delivery_fy:
            continue

        candidates.append({
            "delivery_fy": delivery_fy,
            "url": abs_url,
            "filename": filename,
        })

    candidates.sort(key=lambda r: r["delivery_fy"])
    logger.info(
        "extracted %d main auction ZIP links (min_delivery_fy=%d)",
        len(candidates), min_delivery_fy,
    )
    return candidates


def fetch_index_page(index_url: str) -> str:
    """index ページの HTML を取得する。"""
    logger.info("GET index: %s", index_url)
    r = get(index_url, timeout=60)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# ZIP ダウンロード
# ---------------------------------------------------------------------------


def download_auction_zip(url: str, dest_path: Path) -> bytes:
    """約定結果 ZIP を download して raw ディレクトリに保存。

    既に dest_path が存在する場合は download せず、既存ファイルの bytes を返す。
    """
    if dest_path.exists() and dest_path.stat().st_size > 100:
        logger.info("cache hit: %s (%d bytes)", dest_path, dest_path.stat().st_size)
        return dest_path.read_bytes()

    logger.info("GET: %s", url)
    r = get(url, timeout=60)
    r.raise_for_status()
    content = r.content
    if len(content) < 100:
        raise RuntimeError(
            f"downloaded ZIP is suspiciously small ({len(content)} bytes); "
            f"content preview: {content[:200]!r}"
        )
    # ZIP magic check
    if not content.startswith(b"PK"):
        raise RuntimeError(
            f"downloaded file is not a ZIP archive (magic bytes: {content[:4]!r})"
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(content)
    logger.info("saved raw: %s (%d bytes)", dest_path, len(content))
    return content


# ---------------------------------------------------------------------------
# ZIP + CSV パース (Day 2 probe で実構造完全把握済)
# ---------------------------------------------------------------------------


def parse_auction_zip_csv(
    zip_bytes: bytes,
    cfg: dict,
    delivery_fy: int,
) -> list[dict]:
    """約定結果 ZIP から 11 系列 × 1 年分の long 形式レコードを返す。

    実装ロジック (Day 2 probe で確定):
      1. ZIP を解凍 → 1 枚目の CSV を cp932 で読む
      2. 列構造 [対象実需給年度, エリア, 約定価格[円/kW], エリア毎の約定容量[kW],
                エリア毎の約定総額（経過措置控除後）[円]] を検証
      3. 9 エリア行を読み、area_map で日本語 → 英数 indicator サフィックスに変換
      4. 各エリアの価格 → capacity-main-auction-price-{area} (9 系列)
      5. 9 エリアの容量合計 → capacity-main-auction-volume-total (1 系列)
      6. Σ(価格 × 容量) / Σ(容量) → capacity-main-auction-price-national (1 系列、加重平均)
      合計 11 レコードを返す。

    Args:
        zip_bytes: ZIP ファイルの bytes
        cfg: source_map.yaml の occto-capacity セクション
        delivery_fy: 実需給年度 (e.g. 2024)

    Returns:
        list of {date, indicator_id, region, value} 形式
    """
    area_map = cfg.get("area_map") or {}
    if not area_map:
        logger.error("source_map.yaml: area_map が未定義")
        return []
    csv_encoding = cfg.get("csv_encoding") or "cp932"

    # 1. ZIP 解凍 + CSV 抽出
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            csv_entries = [
                info for info in z.infolist()
                if info.filename.lower().endswith(".csv")
            ]
            if not csv_entries:
                logger.error("FY%d: ZIP に CSV が見つかりません", delivery_fy)
                return []
            if len(csv_entries) > 1:
                # 想定外: probe では 1 ZIP に 1 CSV のみだった
                logger.warning(
                    "FY%d: ZIP に CSV が %d 件 (想定 1 件)、先頭を採用: %s",
                    delivery_fy, len(csv_entries),
                    csv_entries[0].filename.encode("cp437").decode("cp932", errors="replace"),
                )
            with z.open(csv_entries[0]) as f:
                csv_bytes = f.read()
    except zipfile.BadZipFile as e:
        logger.error("FY%d: ZIP open failed: %s", delivery_fy, e)
        return []

    # 2. CSV を cp932 で decode → pandas で読み込み
    try:
        df = pd.read_csv(io.BytesIO(csv_bytes), encoding=csv_encoding, dtype=str)
    except Exception as e:
        logger.error("FY%d: CSV read failed: %s", delivery_fy, e)
        return []

    # 列構造検証
    expected_cols = cfg.get("csv_columns") or []
    actual_cols = list(df.columns)
    if expected_cols and actual_cols != expected_cols:
        logger.warning(
            "FY%d: CSV 列構造が想定と異なる。expected=%s actual=%s",
            delivery_fy, expected_cols, actual_cols,
        )
        # 列名を index で参照するため最低 5 列必要
        if len(actual_cols) < 5:
            logger.error("FY%d: CSV 列数不足 (%d 列)、parse 中止", delivery_fy, len(actual_cols))
            return []

    # 3-5. 9 エリア行を抽出
    date_str = f"{delivery_fy:04d}-04-01"
    source_url = cfg.get("source_url", "")

    # 列名は index ベース (列名揺れに耐性)
    # 0: 対象実需給年度, 1: エリア, 2: 約定価格, 3: 約定容量, 4: 約定総額
    rows: list[dict] = []
    total_volume = 0
    weighted_price_sum = 0  # Σ(価格 × 容量)
    n_areas_found = 0

    for _, row in df.iterrows():
        area_jp = str(row.iloc[1]).strip()
        if not area_jp or area_jp.lower() in {"nan", "none", ""}:
            continue
        area_code = area_map.get(area_jp)
        if area_code is None:
            logger.warning("FY%d: 未知のエリア名 '%s' を skip", delivery_fy, area_jp)
            continue

        # 価格と容量を整数化 (CSV には quotes 付きと無しの混在あり、pandas は文字列として読む)
        try:
            price = int(str(row.iloc[2]).strip().replace(",", ""))
            volume = int(str(row.iloc[3]).strip().replace(",", ""))
        except (ValueError, TypeError) as e:
            logger.warning(
                "FY%d area=%s: 価格/容量 parse 失敗 (price=%r volume=%r): %s",
                delivery_fy, area_jp, row.iloc[2], row.iloc[3], e,
            )
            continue

        # CSV の対象実需給年度欄を検証 (整合性チェック)
        try:
            fy_in_row = int(str(row.iloc[0]).strip())
            if fy_in_row != delivery_fy:
                logger.warning(
                    "FY%d: CSV row の実需給年度 %d が引数 %d と不一致 (filename ベースを優先)",
                    delivery_fy, fy_in_row, delivery_fy,
                )
        except (ValueError, TypeError):
            pass

        # エリア別価格レコード追加
        rows.append({
            "date": date_str,
            "indicator_id": f"capacity-main-auction-price-{area_code}",
            "region": area_code,
            "value": float(price),
        })

        # エリア別約定容量レコード追加 (catalog 133→142、ユウ Q6 要望)
        rows.append({
            "date": date_str,
            "indicator_id": f"capacity-main-auction-volume-{area_code}",
            "region": area_code,
            "value": float(volume),
        })

        total_volume += volume
        weighted_price_sum += price * volume
        n_areas_found += 1

    if n_areas_found == 0:
        logger.error("FY%d: 有効なエリア行が 1 件も見つからない", delivery_fy)
        return []

    if n_areas_found < 9:
        logger.warning(
            "FY%d: エリア行が 9 件揃わず (%d 件のみ)、national/volume-total は部分集計",
            delivery_fy, n_areas_found,
        )

    # 約定容量合計
    rows.append({
        "date": date_str,
        "indicator_id": "capacity-main-auction-volume-total",
        "region": "jp",
        "value": float(total_volume),
    })

    # 全国加重平均価格
    if total_volume > 0:
        national_price = weighted_price_sum / total_volume
        rows.append({
            "date": date_str,
            "indicator_id": "capacity-main-auction-price-national",
            "region": "jp",
            "value": round(national_price, 2),
        })
    else:
        logger.warning("FY%d: 容量合計が 0、national price を skip", delivery_fy)

    logger.info(
        "FY%d parsed: %d areas, total_volume=%d kW, national_avg=%.2f ¥/kW, %d rows",
        delivery_fy, n_areas_found, total_volume,
        weighted_price_sum / total_volume if total_volume > 0 else 0,
        len(rows),
    )
    return rows


def _delivery_fy_to_date(delivery_fy: int) -> str:
    """実需給年度 (FY) を ISO date (YYYY-MM-DD) に変換。

    実需給年度の 4 月 1 日を代表日付として採用 (例: FY2024 → 2024-04-01)。
    """
    return f"{delivery_fy:04d}-04-01"


# ---------------------------------------------------------------------------
# メインパイプライン
# ---------------------------------------------------------------------------


def fetch_all_auctions(cfg: dict, *, backfill: bool, dry_run: bool) -> int:
    """公表済みメインオークションを走査、ZIP を download → parse → CSV 追記。

    backfill=True:  min_delivery_fy (デフォルト 2024) 以降の全年度を走査
    backfill=False: 最新オークションのみ走査 (通常モード)

    Returns:
        int: 新規に追記した行数 (全 indicator 合計)
    """
    index_url = cfg.get("index_url")
    if not index_url:
        logger.error("index_url が source_map.yaml に未設定")
        return 0

    min_fy = int(cfg.get("min_delivery_fy") or 2024)

    # index ページから ZIP リンク一覧を取得
    try:
        index_html = fetch_index_page(index_url)
    except Exception as e:
        logger.error("index fetch failed: %s", e)
        return 0

    all_zips = list_auction_zips(index_html, base_url=index_url, min_delivery_fy=min_fy)

    # 通常モードは最新 1 年度のみ
    if not backfill:
        if all_zips:
            all_zips = all_zips[-1:]
        logger.info("normal mode: keeping latest 1 fiscal year")
    else:
        logger.info("backfill mode: all %d fiscal years from FY%d", len(all_zips), min_fy)

    if dry_run:
        logger.info("--- dry-run: %d target ZIPs ---", len(all_zips))
        for zk in all_zips:
            logger.info("  FY%d: %s", zk["delivery_fy"], zk["filename"])
        return 0

    # --- 実行フェーズ -----------------------------------------------
    processed_dir = PROCESSED_DIR
    raw_dir = RAW_DIR
    processed_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # indicator_id → list[dict] を蓄積 (年度を跨いで concat 可能)
    accumulated: dict[str, list[dict]] = {}

    for zk in all_zips:
        fy = zk["delivery_fy"]
        raw_path = raw_dir / zk["filename"]
        try:
            zip_bytes = download_auction_zip(zk["url"], raw_path)
        except Exception as e:
            logger.error("FY%d download failed: %s", fy, e)
            continue
        time.sleep(SLEEP_BETWEEN_REQUESTS)
        try:
            rows = parse_auction_zip_csv(zip_bytes, cfg, fy)
        except Exception as e:
            logger.error("FY%d parse failed: %s", fy, e)
            continue

        for r in rows:
            accumulated.setdefault(r["indicator_id"], []).append(r)

        logger.info(
            "FY%d: extracted %d rows covering %d indicators",
            fy, len(rows),
            len({r["indicator_id"] for r in rows}),
        )

    # --- CSV 追記 + metadata 書き出し (indicator 単位) -----
    total_new_rows = 0
    source_url = cfg.get("source_url", "")
    for indicator_id, rows in sorted(accumulated.items()):
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["source_url"] = source_url
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
        help="FY2024 以降の全メインオークション約定結果を再取得する",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="ダウンロードは行わず、index ページから ZIP リンク一覧を表示するのみ",
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
