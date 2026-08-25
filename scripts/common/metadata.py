"""
系列メタデータの書き出しヘルパ（D-011 「系列メタデータ・スキーマ v1」実装）。

責務:
- source_map.yaml の 1 ソース分設定 + indicator_id から 19 項目 metadata dict を組み立てる
- data/processed/{domain}/{indicator_id}.metadata.json に UTF-8 JSON で書き出す
- CSV の最終観測日（observation_cutoff）と実行時刻（updated_at）を自動注入する
- Required 10 フィールドの欠落を検出（CI smoke test 用）

呼び出し側（fetch_*.py）は CSV を write_processed したあとに
write_metadata_for_indicator(...) を 1 行呼ぶだけで済む。

フィールド定義は docs/decisions.md の D-011、および
energy-data-platform/docs/metadata-schema-discussion.md §12 を参照。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

logger = logging.getLogger(__name__)


# --- D-011 定数 -----------------------------------------------------------

# Required 11 フィールド（欠落は CI エラー） — D-017 で csv_path 追加 (5/22 ACCEPTED 予定)
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "domain",
    "frequency",
    "unit",
    "source_name",
    "source_url",
    "license",
    "observation_cutoff",
    "updated_at",
    "csv_path",
)

# Recommended 5 フィールド（欠落は CI 警告、ブロックしない）
RECOMMENDED_FIELDS: tuple[str, ...] = (
    "license_url",
    "license_notice",
    "tz",
    "missing_policy",
    "backfill_start",
)

# Optional 7 フィールド（末尾 3 つは D-020② 鮮度監視スキーマ v2）
OPTIONAL_FIELDS: tuple[str, ...] = (
    "publisher",
    "aggregation",
    "notes",
    "depends_on",
    # --- D-020② 鮮度監視スキーマ v2 (2026-08-25) -------------------------
    # observation_cutoff が「何の日付か」を宣言する。
    #   observation … 実際に観測された最終日（既定。従来どおりの解釈）
    #   delivery    … 受渡日 / 受渡年度開始日。将来日付になり得るため、そのまま
    #                 age を測ると負値になり鮮度監視が永久に沈黙する（P1 の原因）。
    "cutoff_semantics",
    # delivery 系列の「公表 → 受渡開始」リードタイム（日）。
    # 実効観測日 = observation_cutoff − delivery_horizon_days として age を測る。
    "delivery_horizon_days",
    # 制度側都合の公表遅延を正常系として吸収する猶予日数（違反判定にのみ加算）。
    "grace_days",
)

ALL_FIELDS: tuple[str, ...] = REQUIRED_FIELDS + RECOMMENDED_FIELDS + OPTIONAL_FIELDS
assert len(ALL_FIELDS) == 23, (
    "D-020 で 23 項目固定 (D-017 の 20 + cutoff_semantics / delivery_horizon_days / grace_days)"
)

# 値候補（CI smoke test で検証）
DOMAIN_VALUES = {
    "power", "fuel", "weather", "finance", "macro",
    "regulation", "tech", "geopolitics", "economy",
    "population", "corp_ir", "international",
    "esg",  # 2026-06-01 北極星 12 ドメイン 唯一未 seed の ESG を seed（EU ETS 検証排出量）
}
FREQUENCY_VALUES = {
    "30min", "daily", "weekly", "monthly", "quarterly", "annual",
}
LICENSE_VALUES = {
    # SPDX 準拠
    "CC-BY-4.0", "CC0-1.0", "MIT", "Apache-2.0", "public-domain",
    # カスタム
    "boj-terms", "jepx-terms", "jma-terms", "meti-terms",
    "mlit-terms", "occto-terms", "eprx-terms", "proprietary",
    "ecb-terms", "estat-terms", "nbs-terms",
    "eea-terms",  # 2026-06-01 EU ETS（EEA/EUTL 再利用ポリシー: 出典明記で商用可 + datahub 加工 PDDL）
    "edinet-terms",  # 2026-06-10 EDINET（金融庁、公共データ利用規約 PDL1.0: 営利含む二次利用可・出典明記。L-063 GO）
    # 2026-08-08 GIO 温室効果ガス排出量データ（国立環境研究所 温室効果ガスインベントリオフィス）。
    # サイト全体の一般著作権条項（複製・頒布に事前許諾が必要）とは別に、
    # 「温室効果ガス排出量データの利用規約」という独立した節があり、そちらが適用される:
    # 無改変の第三者頒布・派生物の作成公表・商用利用すべて明示許諾。ただし
    #   (a) 頒布先へ 3 点（原頒布元 URL / 本規約適用 / 随時更新される旨）を通知する義務
    #   (b) 頒布物にも本規約が引き継がれる（share-alike 的条項 → CC BY 4.0 への再ライセンス不可）
    # があるため CC-BY-4.0 でも政府標準利用規約でもない GIO 独自条件。L-063 = 🟡 条件付き GO。
    "gio-terms",
}
MISSING_POLICY_VALUES = {
    "raw", "forward_fill", "forward_fill_within_month",
    "null", "last_observation", "zero", "interpolate",
}
AGGREGATION_VALUES = {
    "raw", "daily_sum", "daily_mean", "daily_max", "daily_min",
    "monthly_sum", "monthly_mean", "monthly_end", "monthly_high", "monthly_low",
    "area_average", "alias", "derived",
    "annual_auction",  # Phase D 第 1 期 Day 1 (2026-05-20): OCCTO 容量市場 年 1 回オークション
    "annual_mean",  # Phase D (2026-05-23, D-018): EPRX 需給調整 商品別 年間平均落札単価
    "annual_sum",  # 2026-06-01 EU ETS 国別合計（Family B: leaf 部門の国 × 年 合計）
}
# D-020②: observation_cutoff の意味論。未宣言は "observation" 扱い（後方互換）。
CUTOFF_SEMANTICS_VALUES = {"observation", "delivery"}

# frequency 別デフォルトの鮮度 SLA（日数）。
# source_map.yaml の freshness_sla_days で個別 override 可能。
DEFAULT_FRESHNESS_SLA_DAYS: dict[str, int] = {
    "30min": 1,
    "daily": 3,
    "weekly": 10,
    "monthly": 45,
    "quarterly": 150,
    "annual": 540,
}


# --- タイムスタンプ --------------------------------------------------------


def _now_jst_iso() -> str:
    """ISO-8601 の JST タイムスタンプ（秒精度）を返す。"""
    return datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=9))
    ).isoformat(timespec="seconds")


def observation_cutoff_from_df(df: pd.DataFrame) -> str:
    """共通スキーマ DataFrame の date 列の最大値（YYYY-MM-DD）を返す。空なら ""。"""
    if df is None or "date" not in df.columns or len(df) == 0:
        return ""
    s = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return str(s.max())


# --- スキーマ組み立て -----------------------------------------------------


def build_metadata(
    source_cfg: dict,
    indicator_id: str,
    *,
    observation_cutoff: str,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """
    source_map.yaml の 1 ソース分 dict と indicator_id から 19 項目 metadata を組み立てる。

    source_cfg 想定構造:
        source_cfg = cfg["sources"][SOURCE_KEY]
        - ソース共通項目: name / publisher / publisher_url / license / license_url /
                          license_notice / frequency / tz / missing_policy /
                          freshness_sla_days
        - indicators: dict が optional で入る。キーは indicator_id、値は
                      { name, domain, unit, backfill_start, aggregation,
                        notes, depends_on, source_url, ... } などの個別 override。

    indicator 固有項目が source_cfg["indicators"][indicator_id] に無ければ、
    source 共通項目にフォールバック。
    """
    indicators = source_cfg.get("indicators") or {}
    ind = indicators.get(indicator_id) or {}

    def pick(key: str, default=None):
        """indicator 固有 → source 共通 → default の順で引く。"""
        if key in ind and ind[key] is not None:
            return ind[key]
        if key in source_cfg and source_cfg[key] is not None:
            return source_cfg[key]
        return default

    # source_url は個別 → 共通 の順に、共通は source_cfg["publisher_url"] も候補
    source_url = (
        ind.get("source_url")
        or source_cfg.get("source_url")
        or source_cfg.get("publisher_url")
        or ""
    )

    meta: dict[str, Any] = {
        # --- Required 10 ---
        "id": indicator_id,
        "name": ind.get("name") or indicator_id,
        "domain": pick("domain"),
        "frequency": pick("frequency"),
        "unit": pick("unit"),
        "source_name": pick("source_name") or source_cfg.get("name"),
        "source_url": source_url,
        "license": pick("license"),
        "observation_cutoff": observation_cutoff,
        "updated_at": updated_at or _now_jst_iso(),
        # --- Recommended 5 ---
        "license_url": pick("license_url"),
        "license_notice": pick("license_notice", ""),
        "tz": pick("tz", "UTC"),
        "missing_policy": pick("missing_policy"),
        "backfill_start": pick("backfill_start"),
        # --- Optional 7 ---
        "publisher": pick("publisher"),
        "aggregation": pick("aggregation"),
        "notes": pick("notes"),
        "depends_on": ind.get("depends_on"),  # 派生系列のみ埋まる。null 可
        # --- D-020② 鮮度監視スキーマ v2 ---
        "cutoff_semantics": pick("cutoff_semantics", "observation"),
        "delivery_horizon_days": pick("delivery_horizon_days"),
        "grace_days": pick("grace_days"),
    }
    return meta


# --- 書き出し ------------------------------------------------------------


def write_metadata(
    processed_dir: Path,
    indicator_id: str,
    meta: dict,
) -> Path:
    """
    processed_dir / f"{indicator_id}.metadata.json" に UTF-8 + indent=2 で書き出す。
    """
    path = Path(processed_dir) / f"{indicator_id}.metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")
    logger.info("wrote metadata: %s", path)
    return path


def write_metadata_for_indicator(
    processed_dir: Path,
    source_cfg: dict,
    indicator_id: str,
    df: pd.DataFrame,
) -> Path:
    """
    fetch_*.py から呼ぶ便利ショートカット。
    CSV 書き出し直後に 1 行で:
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, df)
    """
    cutoff = observation_cutoff_from_df(df)
    meta = build_metadata(source_cfg, indicator_id, observation_cutoff=cutoff)
    return write_metadata(processed_dir, indicator_id, meta)


# --- 検証 ----------------------------------------------------------------


def _is_nonneg_int(v: Any) -> bool:
    """非負整数か。bool は int のサブクラスだが数値としては受け付けない。"""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def validate_metadata(meta: dict) -> dict[str, list[str]]:
    """
    メタデータの健全性を検査。戻り値:
        {
            "errors":   [...],   # Required 欠落 / スキーマ違反。CI fail
            "warnings": [...],   # Recommended 欠落 / 鮮度オーバー。非 fail
        }
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Required 10 欠落
    for f in REQUIRED_FIELDS:
        v = meta.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            errors.append(f"required field missing: {f}")

    # 値候補チェック
    if meta.get("domain") and meta["domain"] not in DOMAIN_VALUES:
        errors.append(f"domain '{meta['domain']}' is not in DOMAIN_VALUES")
    if meta.get("frequency") and meta["frequency"] not in FREQUENCY_VALUES:
        errors.append(f"frequency '{meta['frequency']}' is not in FREQUENCY_VALUES")
    if meta.get("license") and meta["license"] not in LICENSE_VALUES:
        errors.append(f"license '{meta['license']}' is not in LICENSE_VALUES")
    if meta.get("missing_policy") and meta["missing_policy"] not in MISSING_POLICY_VALUES:
        warnings.append(f"missing_policy '{meta['missing_policy']}' is not in MISSING_POLICY_VALUES")
    if meta.get("aggregation") and meta["aggregation"] not in AGGREGATION_VALUES:
        warnings.append(f"aggregation '{meta['aggregation']}' is not in AGGREGATION_VALUES")

    # --- D-020② 鮮度監視スキーマ v2 -------------------------------------
    semantics = meta.get("cutoff_semantics")
    horizon = meta.get("delivery_horizon_days")
    if semantics is not None and semantics not in CUTOFF_SEMANTICS_VALUES:
        errors.append(
            f"cutoff_semantics '{semantics}' is not in CUTOFF_SEMANTICS_VALUES"
        )
    if semantics == "delivery":
        # 「delivery と宣言だけして offset 不明」は age を実効化できず、
        # cutoff が将来日付のまま監視が沈黙する P1 の再来になるので即 fail。
        if not _is_nonneg_int(horizon):
            errors.append(
                "cutoff_semantics='delivery' requires delivery_horizon_days "
                f"as a non-negative int (got {horizon!r})"
            )
    elif horizon is not None:
        warnings.append(
            f"delivery_horizon_days={horizon!r} is ignored because "
            f"cutoff_semantics is '{semantics or 'observation'}'"
        )
    grace = meta.get("grace_days")
    if grace is not None and not _is_nonneg_int(grace):
        errors.append(f"grace_days must be a non-negative int (got {grace!r})")

    # Recommended 欠落（warning）
    for f in RECOMMENDED_FIELDS:
        v = meta.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            # license_notice だけは空文字を許容（不要な出典あり）
            if f == "license_notice":
                continue
            warnings.append(f"recommended field missing: {f}")

    return {"errors": errors, "warnings": warnings}


def effective_cutoff_age(entry: dict, today: date) -> int | None:
    """
    D-020②: delivery 系列は cutoff − delivery_horizon_days を実効観測日として age を測る。

    cutoff_semantics="delivery" の系列（容量市場・JEPX スポット）は
    observation_cutoff が受渡日 / 受渡年度開始日であり、将来日付になり得る。
    そのまま today − cutoff を取ると負値になり、閾値を永久に超えない
    （＝鮮度監視が沈黙する）ため、公表 → 受渡開始のリードタイム分を巻き戻す。

    observation（未宣言を含む）は horizon 0 となり従来と同一の age を返す。
    cutoff 欠落・parse 不能は None（従来どおり呼び出し側で skip）。

    generate_catalog.py と check_staleness.py の双方から呼ぶ単一実装。
    """
    cutoff = entry.get("observation_cutoff")
    if not cutoff:
        return None
    try:
        cutoff_date = datetime.strptime(str(cutoff), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    horizon = (
        entry.get("delivery_horizon_days")
        if entry.get("cutoff_semantics") == "delivery"
        else 0
    )
    return (today - (cutoff_date - timedelta(days=horizon or 0))).days


def freshness_sla_days(
    source_cfg: dict,
    frequency: str | None,
    indicator_id: str | None = None,
) -> int:
    """
    indicator 固有 > source 共通 > frequency デフォルト の順で解決。
    """
    if indicator_id:
        ind = (source_cfg.get("indicators") or {}).get(indicator_id) or {}
        v = ind.get("freshness_sla_days")
        if v is not None:
            return int(v)
    v = source_cfg.get("freshness_sla_days")
    if v is not None:
        return int(v)
    return DEFAULT_FRESHNESS_SLA_DAYS.get(frequency or "daily", 3)
