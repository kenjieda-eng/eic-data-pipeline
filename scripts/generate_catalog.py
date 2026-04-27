#!/usr/bin/env python3
"""
data/processed/**/*.metadata.json を集約して data/catalog/indicators.json を生成するスクリプト。

D-011「系列メタデータ・スキーマ v1」で決めた案 γ（ハイブリッド保存）の中央側。
- 真実のソース: fetch_*.py が書く data/processed/{domain}/{id}.metadata.json
- このスクリプト: それを CI で決定論的に集約 → data/catalog/indicators.json 1 本
- モック（mockups/index-v2.html）: indicators.json を起動時に 1 fetch → REAL_DATA にマージ

CI smoke test:
- Required 10 欠落は errors に積み、exit 1
- Recommended 5 欠落 / 鮮度オーバーは warnings に積み、exit 0（情報のみ）

使い方:
    python scripts/generate_catalog.py               # 通常
    python scripts/generate_catalog.py --strict      # warnings も exit 1
    python scripts/generate_catalog.py --check-only  # 書き出さず検証のみ

参照:
- docs/decisions.md#D-011
- docs/metadata-schema-discussion.md §12
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.metadata import (  # noqa: E402
    DEFAULT_FRESHNESS_SLA_DAYS,
    REQUIRED_FIELDS,
    freshness_sla_days as resolve_freshness_sla,
    validate_metadata,
)

SOURCE_MAP_PATH = ROOT / "docs" / "source_map.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("generate_catalog")


# --- 収集 ---------------------------------------------------------------


def discover_metadata_files(processed_dir: Path) -> list[Path]:
    """data/processed/**/*.metadata.json をすべて探す。"""
    if not processed_dir.exists():
        return []
    files = sorted(processed_dir.rglob("*.metadata.json"))
    return files


def load_metadata(path: Path) -> dict[str, Any]:
    """metadata.json を読み込む。失敗時は例外を上げる。"""
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def load_source_map() -> dict:
    """docs/source_map.yaml を読み込む。"""
    with SOURCE_MAP_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_indicator_to_source_index(source_map: dict) -> dict[str, dict]:
    """
    indicator_id -> source_cfg のマップを構築する。
    fetch_*.py が freshness_sla_days を metadata.json に書き込む実装が完了するまで、
    catalog 生成側で source_map.yaml を参照して SLA を補填する暫定配線（D-011 phase 0 ギャップ）。
    優先順位: indicators dict（D-011 v2 構造）→ indicator_ids リスト（互換）。
    """
    index: dict[str, dict] = {}
    for source_cfg in (source_map.get("sources") or {}).values():
        for ind_id in (source_cfg.get("indicators") or {}):
            index[ind_id] = source_cfg
        for ind_id in (source_cfg.get("indicator_ids") or []):
            index.setdefault(ind_id, source_cfg)
    return index


# --- 鮮度チェック --------------------------------------------------------


def _now_jst() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))


def freshness_warning(meta: dict) -> str | None:
    """
    observation_cutoff と frequency から鮮度を評価し、
    SLA を超えていたら警告文字列を返す（超えていなければ None）。
    """
    cutoff = meta.get("observation_cutoff")
    frequency = meta.get("frequency") or "daily"
    if not cutoff:
        return None
    try:
        cutoff_dt = datetime.strptime(cutoff, "%Y-%m-%d").replace(
            tzinfo=timezone(timedelta(hours=9))
        )
    except ValueError:
        return f"observation_cutoff '{cutoff}' is not YYYY-MM-DD"
    now_jst = _now_jst()
    age_days = (now_jst.date() - cutoff_dt.date()).days
    sla = DEFAULT_FRESHNESS_SLA_DAYS.get(frequency, 3)
    # meta 自体に freshness_sla_days が埋まっていれば（将来拡張）それを優先
    sla_override = meta.get("freshness_sla_days")
    if isinstance(sla_override, (int, float)) and sla_override > 0:
        sla = int(sla_override)
    if age_days > sla:
        return (
            f"freshness SLA exceeded: age={age_days}d, sla={sla}d "
            f"(frequency={frequency}, cutoff={cutoff})"
        )
    return None


# --- カタログ組み立て ----------------------------------------------------


def build_catalog(indicators: list[dict]) -> dict[str, Any]:
    """
    indicators.json のトップレベル構造を組み立てる。
    sort_keys=False + id 昇順で決定論的に出力。
    """
    # id 昇順で並べる
    sorted_indicators = sorted(indicators, key=lambda m: m.get("id", ""))
    return {
        "version": 1,
        "schema": "D-011",
        "generated_at": _now_jst().isoformat(timespec="seconds"),
        "indicator_count": len(sorted_indicators),
        "indicators": sorted_indicators,
    }


def write_catalog(catalog: dict, out_path: Path) -> None:
    """indicators.json を UTF-8 + indent=2 で書き出す。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=False)
    out_path.write_text(text + "\n", encoding="utf-8")
    logger.info("wrote catalog: %s (%d indicators)", out_path, catalog["indicator_count"])


# --- メイン --------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="warnings も含めて exit 1 にする",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="indicators.json を書き出さず、検証のみ",
    )
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=ROOT / "data" / "processed",
        help="走査する processed ディレクトリ（既定: data/processed）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / "catalog" / "indicators.json",
        help="出力先（既定: data/catalog/indicators.json）",
    )
    args = parser.parse_args()

    files = discover_metadata_files(args.processed_dir)
    if not files:
        logger.error("no metadata.json files found under %s", args.processed_dir)
        return 1

    source_map = load_source_map()
    indicator_to_source = build_indicator_to_source_index(source_map)

    indicators: list[dict] = []
    total_errors: list[str] = []
    total_warnings: list[str] = []

    for path in files:
        try:
            meta = load_metadata(path)
        except Exception as e:
            total_errors.append(f"{path}: failed to load: {e}")
            continue

        result = validate_metadata(meta)
        for err in result["errors"]:
            total_errors.append(f"{path.name}: {err}")
        for warn in result["warnings"]:
            total_warnings.append(f"{path.name}: {warn}")

        # source_map.yaml に基づき freshness_sla_days を解決して meta に注入。
        # fetch_*.py が metadata.json に直接書き込む実装が入るまでの暫定配線。
        if "freshness_sla_days" not in meta:
            ind_id = meta.get("id")
            src_cfg = indicator_to_source.get(ind_id) if ind_id else None
            if src_cfg is not None:
                meta["freshness_sla_days"] = resolve_freshness_sla(
                    src_cfg, meta.get("frequency"), ind_id
                )

        # 鮮度警告（注入された SLA を含めて評価）
        fw = freshness_warning(meta)
        if fw:
            total_warnings.append(f"{path.name}: {fw}")

        indicators.append(meta)

    # 集計
    n = len(indicators)
    logger.info("collected %d metadata.json files", n)
    if total_warnings:
        logger.warning("--- warnings (%d) ---", len(total_warnings))
        for w in total_warnings:
            logger.warning("  %s", w)
    if total_errors:
        logger.error("--- errors (%d) ---", len(total_errors))
        for e in total_errors:
            logger.error("  %s", e)

    # 書き出し
    catalog = build_catalog(indicators)
    if not args.check_only:
        write_catalog(catalog, args.out)
    else:
        logger.info("--check-only: skipped write")

    # exit コード
    if total_errors:
        logger.error("exit 1 due to %d errors", len(total_errors))
        return 1
    if args.strict and total_warnings:
        logger.error("exit 1 due to %d warnings (--strict)", len(total_warnings))
        return 1

    logger.info("OK: %d indicators, %d warnings", n, len(total_warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
