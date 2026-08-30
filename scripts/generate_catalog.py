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
    DEFAULT_UPDATE_SCHEDULE,
    REQUIRED_FIELDS,
    axis2_violation,
    derive_coverage,
    effective_cutoff_age,
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


def derive_csv_path(meta_file: Path, processed_dir: Path) -> str:
    """
    metadata.json ファイルのパスから対応する CSV パスを導出 (D-017)。
    例: data/processed/enecho-power/meti-gen-solar.metadata.json
        → "data/processed/enecho-power/meti-gen-solar.csv"

    返り値は repo ルートからの相対パス (POSIX 形式、Windows 環境でも `/` 区切り)。
    """
    # processed_dir.parent = repo root の "data" 階層、その親 (ROOT) からの相対パスにする
    relative = meta_file.relative_to(processed_dir.parent.parent)
    csv_rel = str(relative).replace(".metadata.json", ".csv")
    return csv_rel.replace("\\", "/")


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

    D-020②: age は effective_cutoff_age() で測る。cutoff_semantics="delivery"
    の系列は cutoff（受渡日 / 受渡年度開始日）から delivery_horizon_days を
    巻き戻した実効観測日が基準になる。違反判定には grace_days を加算し、
    制度側都合の公表遅延を正常系として吸収する。
    observation（未宣言を含む）は horizon 0 / grace 0 で従来と同一挙動。
    """
    cutoff = meta.get("observation_cutoff")
    frequency = meta.get("frequency") or "daily"
    if not cutoff:
        return None
    try:
        datetime.strptime(cutoff, "%Y-%m-%d")
    except (ValueError, TypeError):
        return f"observation_cutoff '{cutoff}' is not YYYY-MM-DD"
    age_days = effective_cutoff_age(meta, _now_jst().date())
    if age_days is None:
        return None
    sla = DEFAULT_FRESHNESS_SLA_DAYS.get(frequency, 3)
    # meta 自体に freshness_sla_days が埋まっていれば（将来拡張）それを優先
    sla_override = meta.get("freshness_sla_days")
    if isinstance(sla_override, (int, float)) and sla_override > 0:
        sla = int(sla_override)
    semantics = meta.get("cutoff_semantics") or "observation"
    horizon = meta.get("delivery_horizon_days") if semantics == "delivery" else 0
    grace = meta.get("grace_days") or 0
    if age_days > sla + grace:
        return (
            f"freshness SLA exceeded: effective_age={age_days}d "
            f"(semantics={semantics}, horizon={horizon or 0}, grace={grace}), "
            f"sla={sla}d, cutoff={cutoff} (frequency={frequency})"
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
        "version": 2,
        "schema": "D-020",
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
    now = _now_jst()  # 軸2 判定の基準時刻（全系列で同一時刻を使う）

    for path in files:
        try:
            meta = load_metadata(path)
        except Exception as e:
            total_errors.append(f"{path}: failed to load: {e}")
            continue

        # D-017: csv_path を自動付与 (validate_metadata の REQUIRED_FIELDS チェック前に注入)
        meta["csv_path"] = derive_csv_path(path, args.processed_dir)

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

        # D-020②: cutoff_semantics / delivery_horizon_days / grace_days も
        # source_map.yaml から補填する（freshness_sla_days と同じ暫定配線）。
        # fetch_*.py が書いた metadata.json に既に入っていれば触らない。
        # 未宣言のソースは observation = 従来どおりの age 解釈。
        if "cutoff_semantics" not in meta:
            ind_id = meta.get("id")
            src_cfg = indicator_to_source.get(ind_id) if ind_id else None
            src_cfg = src_cfg or {}
            meta["cutoff_semantics"] = src_cfg.get("cutoff_semantics") or "observation"
            meta["delivery_horizon_days"] = src_cfg.get("delivery_horizon_days")
            meta["grace_days"] = src_cfg.get("grace_days")

        # D-020④(c): update_schedule（軸2 = workflow 周期）も同じ暫定配線で補填する。
        # source_map.yaml に宣言が無いソースは既定 {"kind":"interval","days":7} を
        # **実体化して** 入れる（null のまま置かない）。catalog を自己記述に保ち、
        # 下流が「未宣言のときの既定値」を各自で持たなくて済むようにするため。
        if "update_schedule" not in meta:
            ind_id = meta.get("id")
            src_cfg = indicator_to_source.get(ind_id) if ind_id else None
            src_cfg = src_cfg or {}
            meta["update_schedule"] = (
                src_cfg.get("update_schedule") or dict(DEFAULT_UPDATE_SCHEDULE)
            )

        # D-020③: 収録範囲を CSV の実データから導出して注入する。
        # 人手でも fetcher の metadata.json でも書かない（生成時導出のみ）。
        # cutoff_semantics / frequency 確定後に呼ぶ必要がある
        # （delivery + annual のときだけ label が FY 表記になるため）。
        meta["coverage"] = derive_coverage(ROOT / meta["csv_path"], meta)
        if meta["coverage"] is None:
            total_warnings.append(f"coverage underivable: {meta.get('id') or path.name}")

        # 鮮度警告（注入された SLA / D-020 セマンティクスを含めて評価）= 軸1
        fw = freshness_warning(meta)
        if fw:
            total_warnings.append(f"{path.name}: {fw}")

        # D-020④(c) 軸2: updated_at が update_schedule どおり前進しているか
        # （= workflow が回っているか）。soft 先行のため warning 止まりで、
        # exit コードには影響しない（hard 化は D-020⑤）。
        axis2 = axis2_violation(meta, now)
        if axis2:
            total_warnings.append(f"{path.name}: {axis2}")

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
