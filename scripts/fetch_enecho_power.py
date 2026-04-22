#!/usr/bin/env python3
"""資源エネルギー庁 電力調査統計 月次データ（Phase 2-A 第 1 弾、12 系列）

---------------------------------------------------------------------
ステータス: **SKELETON（Day 1 設計）** — 実装は Day 2 以降
---------------------------------------------------------------------

設計議事録: `docs/data-pipeline-phase2-a-power-discussion.md`

ターゲット 12 系列（すべて月次、2012-01 バックフィル）:
  発電 8: meti-gen-total / -thermal / -hydro / -nuclear / -solar / -wind /
          -geothermal / -biomass
  需要 3: meti-demand-total / -lights / -power
  派生 1: meti-renewables-share（他系列から fetch 後に計算）

処理フロー:
  1. INDEX_URL から XLSX のハイパーリンクを抽出（requests + BeautifulSoup）
  2. ファイル名から年月を推定し、未取得のものだけ download
  3. openpyxl / pandas.read_excel で sheet_hints に一致するシートを特定
  4. row_labels でラベル駆動の値抽出（wide → long 変換）
  5. indicator_id ごとに processed CSV に追記、metadata.json を書き出し
  6. meti-renewables-share は他系列から計算（fetch 後に derive ステップ）

TODO（Day 2 以降で解決）:
  - XLSX のレイアウトが年によって変わる可能性があるので、複数年ぶんの
    サンプルで label anchor のロバスト性を検証
  - 「総計」と「合計」のどちらが使われるかが表によって違うため、
    row_labels はリスト（最初に見つかったものを採用）とした
  - 単位が表示ラベルに含まれるケース（「火力計 (100 万 kWh)」など）にも対応
  - xls（旧形式）ファイルは当面サポートしない（2012 以降は XLSX のみと割り切り）
  - metadata.py の AGGREGATION_VALUES に 'monthly_sum' / 'derived' / LICENSE_VALUES に
    'meti-terms' が含まれているか要確認。不足していれば Day 2 で追加する
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

# 下記 import は Day 2 以降で有効化
# import pandas as pd
# import requests
# import yaml
# from bs4 import BeautifulSoup

# 共通ライブラリ
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

logger = logging.getLogger("fetch_enecho_power")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

SOURCE_ID = "enecho-power"
SOURCE_MAP_PATH = REPO_ROOT / "docs" / "source_map.yaml"
DATA_ROOT = REPO_ROOT / "data"
PROCESSED_DIR = DATA_ROOT / "processed" / "enecho-power"
RAW_DIR = DATA_ROOT / "raw" / "enecho-power"

INDEX_URL = "https://www.enecho.meti.go.jp/statistics/electric_power/ep002/results.html"
USER_AGENT = "Mozilla/5.0 (compatible; EIC-Data-Pipeline/1.0; +https://github.com/kenjieda-eng/eic-data-pipeline)"

# 1 リクエストごとのスリープ（METI サーバへの配慮、1.5 秒）
SLEEP_BETWEEN_REQUESTS = 1.5

# ---------------------------------------------------------------------------
# ユーティリティ（骨組みのみ、Day 2 で実装）
# ---------------------------------------------------------------------------


def load_source_cfg() -> dict:
    """source_map.yaml から enecho-power セクションを読み込む。"""
    # TODO Day 2: yaml.safe_load で読み込み、"sources" → SOURCE_ID を返す
    raise NotImplementedError("Day 2 実装予定")


def list_xlsx_links(index_html: str) -> list[dict]:
    """index ページの HTML から月次 XLSX のリンクを抽出する。

    Returns:
        list of dict: [{ "year_month": "2026-02", "url": "...", "filename": "..." }, ...]
    """
    # TODO Day 2: BeautifulSoup で a.href を走査、file_hint_keywords で filter、
    #              リンクテキストから年月を推定。正規表現複数パターン用意。
    raise NotImplementedError("Day 2 実装予定")


def download_xlsx(url: str, dest_path: Path) -> bytes:
    """XLSX を download して raw ディレクトリに保存、bytes を返す。"""
    # TODO Day 2: requests.get → dest_path.write_bytes → return bytes
    raise NotImplementedError("Day 2 実装予定")


def parse_generation_sheet(xlsx_bytes: bytes, cfg: dict) -> dict[str, Optional[float]]:
    """発電実績シートから 8 系列の月次値を抽出。

    Args:
        xlsx_bytes: XLSX ファイルの bytes
        cfg: source_map.yaml の enecho-power セクション
    Returns:
        { "meti-gen-total": 95234.5, "meti-gen-thermal": ..., ... }
        値が見つからなければ None（欠測）
    """
    # TODO Day 2:
    #   1. pandas.read_excel with sheet_name=cfg["sheet_hints"]["generation_total"]
    #   2. ラベル列を走査して cfg["row_labels"][key] の最初のマッチを採用
    #   3. 月次の value 列（通常は「当月」列）を返す
    raise NotImplementedError("Day 2 実装予定")


def parse_demand_sheet(xlsx_bytes: bytes, cfg: dict) -> dict[str, Optional[float]]:
    """販売電力量シートから 3 系列（total/lights/power）の月次値を抽出。"""
    # TODO Day 2: parse_generation_sheet と同様の構造
    raise NotImplementedError("Day 2 実装予定")


def derive_renewables_share(month_values: dict[str, float]) -> Optional[float]:
    """meti-renewables-share を他系列から計算する。

    (太陽光 + 風力 + 地熱 + 水力 + バイオマス) / 総発電量 × 100
    """
    need = ["meti-gen-solar", "meti-gen-wind", "meti-gen-geothermal",
            "meti-gen-hydro", "meti-gen-biomass", "meti-gen-total"]
    if not all(k in month_values and month_values[k] is not None for k in need):
        return None
    renewables = (month_values["meti-gen-solar"]
                  + month_values["meti-gen-wind"]
                  + month_values["meti-gen-geothermal"]
                  + month_values["meti-gen-hydro"]
                  + month_values["meti-gen-biomass"])
    total = month_values["meti-gen-total"]
    if total <= 0:
        return None
    return round(renewables / total * 100, 3)


# ---------------------------------------------------------------------------
# メインパイプライン
# ---------------------------------------------------------------------------


def fetch_all_months(cfg: dict, backfill: bool, since_ym: Optional[str]) -> int:
    """公表済み全月を走査、未取得の月だけ download & parse → CSV 追記。

    Returns:
        int: 新規に追記した行数
    """
    # TODO Day 2:
    #   1. list_xlsx_links で公表ファイル一覧を取得
    #   2. 既存の processed CSV（meti-gen-total.csv など）から max date を取得
    #   3. 未取得の月だけ download → parse_generation_sheet + parse_demand_sheet
    #   4. indicator_id ごとに CSV に追記（long 形式: date, indicator_id, region, value, source_url）
    #   5. derive_renewables_share を計算して meti-renewables-share.csv に追記
    #   6. write_metadata_for_indicator を各 indicator_id について呼び出し
    raise NotImplementedError("Day 2 実装予定")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch METI 電力調査統計 monthly data")
    parser.add_argument("--backfill", action="store_true",
                        help="2012-01 から全期間を再取得する")
    parser.add_argument("--since", type=str, default=None,
                        help="指定月から取得（YYYY-MM、例: 2024-01）")
    parser.add_argument("--dry-run", action="store_true",
                        help="ダウンロードせず、リンク一覧だけを表示")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    logger.warning(
        "fetch_enecho_power.py is a SKELETON (Phase 2-A Day 1). "
        "Real implementation lands in Day 2. Exiting 0 without side effects."
    )
    # TODO Day 2: 以下を有効化
    # cfg = load_source_cfg()
    # PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    # RAW_DIR.mkdir(parents=True, exist_ok=True)
    # added = fetch_all_months(cfg, backfill=args.backfill, since_ym=args.since)
    # logger.info("done: %d new rows appended", added)
    return 0


if __name__ == "__main__":
    sys.exit(main())
