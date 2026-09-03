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

import calendar
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
    # --- D-020③ 収録範囲の機械可読化 (2026-08-26) ------------------------
    # {first,last,count,label_first,label_last} を catalog 生成時に CSV から導出する。
    # 人手でも fetcher でも書かない。下流が「FY2024-FY2029」等を文言に焼き込むと
    # 系列が伸びても表示は壊れず陳腐化に気付けないため（D-020 P3）。
    "coverage",
    # --- D-020④(c) 軸2: パイプライン生存監視 (2026-08-30) -----------------
    # workflow が「回っているか」の周期を宣言する。データの頻度（frequency）でも
    # 観測日の鮮度（軸1 = observation_cutoff）でもなく、updated_at が前進し続けて
    # いるかだけを測るための軸（D-020 §2.2）。
    #   {"kind": "interval", "days": N}
    #       … N 日ごとに回る workflow（nightly 系は既定の 7）。
    #   {"kind": "window", "months": [6], "grace_days": 45}
    #       … 毎年決まった月に回る workflow（年次取りまとめ等）。
    #   {"kind": "window", "next_expected": "2026-12-15", "grace_days": 45}
    #       … 次回の公表期日が判っている単発運用（回ごとにリードタイムが動くもの）。
    # 未宣言のソースは catalog 生成時に既定 {"kind":"interval","days":7} が実体化される。
    "update_schedule",
)

ALL_FIELDS: tuple[str, ...] = REQUIRED_FIELDS + RECOMMENDED_FIELDS + OPTIONAL_FIELDS
assert len(ALL_FIELDS) == 25, (
    "D-020 で 25 項目固定 (D-017 の 20 + cutoff_semantics / delivery_horizon_days / grace_days"
    " + coverage (D-020③、catalog 生成時導出) + update_schedule (D-020④(c) 軸2))"
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

# --- D-020④(c) 軸2 update_schedule ---------------------------------------
# kind とその許容キー。余剰キーは validate_metadata で error（宣言ミスを
# 「静かに無視」して監視が沈黙するのを防ぐ）。
UPDATE_SCHEDULE_KINDS = {"interval", "window"}
UPDATE_SCHEDULE_KEYS: dict[str, frozenset[str]] = {
    "interval": frozenset({"kind", "days"}),
    "window": frozenset({"kind", "months", "next_expected", "grace_days"}),
}
# 未宣言ソースの既定。nightly workflow は毎日回るが、単発の失敗や祝日運休で
# 数日空くことはあるため 7 日を「明らかに止まっている」の線とする。
DEFAULT_UPDATE_SCHEDULE: dict[str, Any] = {"kind": "interval", "days": 7}

# --- D-020④(d) depends_on: 派生系列の継続判定 ------------------------------
# 派生と依存先は同じ run で書かれるため、書き順による数秒の前後は正常。
# 検出したい故障は「片側だけ数日以上更新されない」なので 24h を許容幅にする。
DEPENDS_ON_TOLERANCE_SECONDS = 86400

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
        # --- D-020④(c) 軸2 生存監視 ---
        # 未宣言（None）は「既定 interval 7」として catalog 生成時に実体化される。
        "update_schedule": pick("update_schedule"),
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


# --- D-020④ 生存信号としての updated_at ------------------------------------


def observation_cutoff_from_csv(csv_path: Path) -> str:
    """
    既存 processed CSV の date 列の最大値（YYYY-MM-DD）を返す。

    ファイル不在・date 列なし・行ゼロ・パース不能など、値を確定できない場合は
    一律 "" を返す（呼び出し側は "" を「metadata を書けない」の合図として扱う）。
    """
    try:
        df = pd.read_csv(Path(csv_path), usecols=["date"])
        s = pd.to_datetime(df["date"], errors="coerce").dropna()
        if len(s) == 0:
            return ""
        return str(s.dt.strftime("%Y-%m-%d").max())
    except Exception as e:  # noqa: BLE001 — 読めない理由を問わず "" に倒す
        logger.debug("observation_cutoff_from_csv failed for %s: %s", csv_path, e)
        return ""


def write_metadata_for_expected_indicators(
    processed_dir: Path,
    source_cfg: dict,
    expected_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """
    D-020④: フェッチには成功したが今回は行がゼロだった indicator の metadata を
    書き直す（updated_at = 実行時刻、observation_cutoff は既存 CSV から再導出）。

    従来の updated_at は「行が書かれた時刻」だったため、行が生成されない期間を
    持つ indicator（積雪の無降雪期・降水の乾燥期など）は metadata が凍結し、
    パイプラインが生きているのか死んでいるのか区別できなかった（D-020 §9.3）。
    ここを通すことで updated_at は「系列が確認された時刻」= 純粋な生存信号になる。

    書かない（skipped に入る）条件:
      - 既存 CSV が無い id（catalog 登録は CSV 実在が前提。metadata だけ先に
        生えると下流が実体のない系列を掴む）
      - observation_cutoff が "" になる id（CSV が読めない / 行ゼロ）

    ⚠️ 呼び出し側の責務: expected_ids には「今回フェッチに成功した範囲」の id
    のみを渡すこと。フェッチが失敗した範囲まで渡すと、「接続はされているが
    失敗し続けている」故障（D-020 §2.4 軸2）の updated_at が進み続け、
    鮮度監視が永久に沈黙する。行ゼロの refresh は必ず成功範囲に限定する。

    戻り値: (written_ids, skipped_ids)。書き出す内容は build_metadata +
    write_metadata による通常経路と同一の 22 項目。
    """
    processed_dir = Path(processed_dir)
    written: list[str] = []
    skipped: list[str] = []
    for indicator_id in expected_ids:
        csv_path = processed_dir / f"{indicator_id}.csv"
        if not csv_path.exists():
            skipped.append(indicator_id)
            continue
        cutoff = observation_cutoff_from_csv(csv_path)
        if not cutoff:
            skipped.append(indicator_id)
            continue
        meta = build_metadata(source_cfg, indicator_id, observation_cutoff=cutoff)
        write_metadata(processed_dir, indicator_id, meta)
        written.append(indicator_id)
    return written, skipped


# --- D-020③ 収録範囲の導出 ------------------------------------------------

# coverage が持つキー。過不足は validate_metadata で error。
COVERAGE_FIELDS: tuple[str, ...] = (
    "first",
    "last",
    "count",
    "label_first",
    "label_last",
)


def _fy_label(date_str: str, entry: dict) -> str:
    """
    収録範囲の端点ラベルを導出する。

    cutoff_semantics="delivery" かつ frequency="annual"（= 容量市場のような
    受渡年度もの）は「2024-04-01」ではなく「FY2024」が業務上の呼び名なので
    年度表記にする。4 月始まりなので month<4 は前年度に倒す。
    それ以外は ISO 日付文字列のまま返す。
    """
    if entry.get("cutoff_semantics") != "delivery" or entry.get("frequency") != "annual":
        return date_str
    d = date.fromisoformat(date_str)
    return f"FY{d.year if d.month >= 4 else d.year - 1}"


def derive_coverage(csv_path: Path, entry: dict) -> dict | None:
    """
    CSV の実データから収録範囲 {first,last,count,label_first,label_last} を導出する。

    D-020③: 収録範囲は人手でも fetcher でも書かず、catalog 生成時に必ずここで
    導出する。下流が「FY2024-FY2029」等を文言に焼き込むと、系列が伸びても表示は
    壊れないまま陳腐化するため（D-020 P3）。

    entry は cutoff_semantics / frequency 解決後のメタデータ（ラベル導出に使う）。
    CSV 不在・データ行ゼロのときは None を返す（呼び出し側で warning）。
    """
    if not csv_path.exists():
        return None
    df = pd.read_csv(csv_path)
    if "date" not in df.columns or len(df) == 0:
        return None
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    if len(s) == 0:
        return None
    s = s.dt.strftime("%Y-%m-%d")
    first, last = str(s.min()), str(s.max())
    return {
        "first": first,
        "last": last,
        "count": int(len(df)),
        "label_first": _fy_label(first, entry),
        "label_last": _fy_label(last, entry),
    }


# --- 検証 ----------------------------------------------------------------


def _is_nonneg_int(v: Any) -> bool:
    """非負整数か。bool は int のサブクラスだが数値としては受け付けない。"""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_iso_date(v: Any) -> bool:
    """YYYY-MM-DD 形式の文字列か。"""
    if not isinstance(v, str):
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


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

    # --- D-020③ coverage ------------------------------------------------
    # catalog 生成時に derive_coverage() が注入する。fetcher 由来の metadata.json
    # には通常存在しないため、「あるときだけ」形状を検査する。
    cov = meta.get("coverage")
    if cov is not None:
        if not isinstance(cov, dict):
            errors.append(f"coverage must be a dict (got {type(cov).__name__})")
        else:
            missing = [k for k in COVERAGE_FIELDS if k not in cov]
            if missing:
                errors.append(f"coverage missing keys: {', '.join(missing)}")
            extra = sorted(k for k in cov if k not in COVERAGE_FIELDS)
            if extra:
                errors.append(f"coverage has unexpected keys: {', '.join(extra)}")
            count = cov.get("count")
            if not (_is_nonneg_int(count) and count >= 1):
                errors.append(f"coverage.count must be a positive int (got {count!r})")
            first, last = cov.get("first"), cov.get("last")
            if not (isinstance(first, str) and first and isinstance(last, str) and last):
                if not missing:
                    errors.append(
                        f"coverage.first / coverage.last must be non-empty strings "
                        f"(got {first!r} / {last!r})"
                    )
            elif first > last:
                errors.append(f"coverage.first ({first}) must be <= coverage.last ({last})")

    # --- D-020④(c) update_schedule（軸2） ---------------------------------
    # source_map.yaml で宣言したソースだけが持つ（未宣言は catalog 生成時に
    # 既定 interval 7 が注入される）。よって「あるときだけ」形状を検査する。
    sched = meta.get("update_schedule")
    if sched is not None:
        if not isinstance(sched, dict):
            errors.append(
                f"update_schedule must be a dict (got {type(sched).__name__})"
            )
        else:
            kind = sched.get("kind")
            if kind not in UPDATE_SCHEDULE_KINDS:
                errors.append(
                    f"update_schedule.kind '{kind}' is not in UPDATE_SCHEDULE_KINDS"
                )
            elif kind == "interval":
                days = sched.get("days")
                if not (_is_nonneg_int(days) and days >= 1):
                    errors.append(
                        f"update_schedule.days must be a positive int (got {days!r})"
                    )
            else:  # window
                months = sched.get("months")
                next_expected = sched.get("next_expected")
                if months is None and next_expected is None:
                    errors.append(
                        "update_schedule kind='window' requires months or next_expected"
                    )
                if months is not None:
                    if not (isinstance(months, list) and months):
                        errors.append(
                            f"update_schedule.months must be a non-empty list "
                            f"(got {months!r})"
                        )
                    elif not all(
                        _is_nonneg_int(m) and 1 <= m <= 12 for m in months
                    ):
                        errors.append(
                            f"update_schedule.months must be ints in 1..12 "
                            f"(got {months!r})"
                        )
                if next_expected is not None and not _is_iso_date(next_expected):
                    errors.append(
                        f"update_schedule.next_expected must be YYYY-MM-DD "
                        f"(got {next_expected!r})"
                    )
            if isinstance(kind, str) and kind in UPDATE_SCHEDULE_KINDS:
                grace_s = sched.get("grace_days")
                if grace_s is not None and not _is_nonneg_int(grace_s):
                    errors.append(
                        f"update_schedule.grace_days must be a non-negative int "
                        f"(got {grace_s!r})"
                    )
                allowed = UPDATE_SCHEDULE_KEYS[kind]
                extra_s = sorted(k for k in sched if k not in allowed)
                if extra_s:
                    errors.append(
                        f"update_schedule has unexpected keys: {', '.join(extra_s)}"
                    )

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


# --- D-020④(c) 軸2: パイプライン生存監視 --------------------------------


def _parse_updated_at(value: Any, now: datetime) -> datetime | None:
    """updated_at（ISO-8601 文字列）を now と比較可能な datetime に正規化する。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    # tz 有無を now 側に揃える（catalog の updated_at は JST aware、テストの now は
    # naive で渡されることがある）。
    if dt.tzinfo is None and now.tzinfo is not None:
        dt = dt.replace(tzinfo=now.tzinfo)
    elif dt.tzinfo is not None and now.tzinfo is None:
        dt = dt.replace(tzinfo=None)
    return dt


def _latest_completed_window(months: list[int], today: date) -> date | None:
    """
    months で宣言された対象月のうち、today 時点で「完全に過ぎた」直近の月の
    初日を返す。候補が無ければ None。
    """
    for year in (today.year, today.year - 1, today.year - 2):
        starts = []
        for m in sorted(set(months), reverse=True):
            last_day = calendar.monthrange(year, m)[1]
            if date(year, m, last_day) < today:
                starts.append(date(year, m, 1))
        if starts:
            return max(starts)
    return None


def axis2_violation(entry: dict, now: datetime) -> str | None:
    """
    D-020④(c) 軸2: updated_at が update_schedule どおりに前進しているかを判定する。

    ⚠️ 軸2は **workflow 周期の監視**。データ頻度（何日おきに新しい観測が出るか）は
    軸1（observation_cutoff / freshness SLA）の担当であり、ここでは一切見ない。
    軸2が答えるのは「パイプラインがまだ回っているか」だけ。両者を混ぜると、
    データ側の自然な空白（無降雪期・年次公表の待ち時間）で生存監視まで沈黙する
    ／逆に生きているのに鮮度違反で赤くなる、という取り違えが起きる（D-020 §2.2）。

    未宣言のエントリは既定 {"kind": "interval", "days": 7} で判定する。
    違反なら理由文字列、違反なしなら None を返す。
    """
    sched = entry.get("update_schedule") or DEFAULT_UPDATE_SCHEDULE
    updated = _parse_updated_at(entry.get("updated_at"), now)
    if updated is None:
        return "updated_at unparsable"

    kind = sched.get("kind")
    today = now.date()
    updated_date = updated.date()

    if kind == "window":
        grace = sched.get("grace_days") or 0
        next_expected = sched.get("next_expected")
        if next_expected:
            try:
                ne = date.fromisoformat(str(next_expected))
            except ValueError:
                return f"update_schedule.next_expected unparsable: {next_expected!r}"
            due = ne + timedelta(days=grace)
            if today > due and updated_date < ne:
                return (
                    f"axis2: updated_at {updated_date} predates next_expected "
                    f"{ne} (due {due} = +{grace}d grace) (pipeline liveness)"
                )
            return None
        months = sched.get("months") or []
        win_start = _latest_completed_window(list(months), today)
        if win_start is None:
            return None
        last_day = calendar.monthrange(win_start.year, win_start.month)[1]
        anchor = date(win_start.year, win_start.month, last_day) + timedelta(days=grace)
        if today > anchor and updated_date < win_start:
            return (
                f"axis2: updated_at {updated_date} predates the "
                f"{win_start:%Y-%m} window (anchor {anchor} = month end "
                f"+{grace}d grace) (pipeline liveness)"
            )
        return None

    # interval（既定）
    days = sched.get("days")
    if not (_is_nonneg_int(days) and days >= 1):
        return f"update_schedule.days invalid: {days!r}"
    stalled = (now - updated).days
    if stalled > days:
        return (
            f"axis2: updated_at stalled {stalled}d > interval {days}d "
            f"(pipeline liveness)"
        )
    return None


# --- D-020④(d) depends_on: 派生系列の継続判定 ------------------------------


def depends_on_violation(entry: dict, by_id: dict[str, dict]) -> str | None:
    """
    D-020④(d): 派生系列の継続判定。depends_on が無ければ None。

    派生系列（比率・シェア・合算）は入力が改訂されると再計算が要るが、再計算が
    漏れてもエラーにならず値も表示され、静かに古い入力に基づいた値が残る
    （軸2 と同型の「沈黙」）。D-011 の depends_on を使い、派生の updated_at が
    依存先より古ければ警告する（D-020 §8.3.1）。

    - 依存先 id が by_id に無い → "depends_on refers to unknown id: X"（宣言ミス）
    - 依存先の updated_at の最大値 − 派生の updated_at > 24h →
      "derived updated_at YYYY-MM-DD lags dependency <id> (YYYY-MM-DD) by Nd"
    - updated_at が parse 不能 → "updated_at unparsable"

    軸2（全体停止）とは別担当: 依存元も派生も同じく古い場合は軸2が拾う。
    ここは「片側だけ更新された不整合」だけを見る（§8.3.1）。

    by_id は id → catalog エントリ。全 metadata を読み終えた後にしか作れないため、
    収集ループ内ではなく二段目で呼ぶこと（依存先が後から読まれ得る）。
    """
    deps = entry.get("depends_on")
    if not deps:
        return None
    if isinstance(deps, str):
        deps = [deps]
    elif isinstance(deps, (list, tuple)):
        deps = list(deps)
    else:
        return f"depends_on unusable: expected id or list of ids, got {type(deps).__name__}"

    # 宣言ミスを先に潰す。存在しない id を指したまま「違反 0」で緑になるのが
    # いちばん危ない沈黙のため、比較より前に落とす。
    for dep_id in deps:
        if dep_id not in by_id:
            return f"depends_on refers to unknown id: {dep_id}"

    raw = entry.get("updated_at")
    if not isinstance(raw, str) or not raw.strip():
        return "updated_at unparsable"
    try:
        derived = datetime.fromisoformat(raw.strip())
    except ValueError:
        return "updated_at unparsable"

    # 依存先のうち最も新しいものと比べる（1 つでも新しければ再計算が要る）。
    latest_id: str | None = None
    latest_dt: datetime | None = None
    for dep_id in deps:
        # derived を基準に tz 有無を揃える（catalog は JST aware、テストは naive）。
        dep_dt = _parse_updated_at(by_id[dep_id].get("updated_at"), derived)
        if dep_dt is None:
            return f"dependency {dep_id} updated_at unparsable"
        if latest_dt is None or dep_dt > latest_dt:
            latest_dt, latest_id = dep_dt, dep_id

    if latest_dt is None:
        return None
    lag = (latest_dt - derived).total_seconds()
    if lag > DEPENDS_ON_TOLERANCE_SECONDS:
        return (
            f"derived updated_at {derived.date()} lags dependency {latest_id} "
            f"({latest_dt.date()}) by {int(lag // 86400)}d"
        )
    return None


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
