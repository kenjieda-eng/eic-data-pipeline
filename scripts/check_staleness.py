#!/usr/bin/env python3
"""
scripts/check_staleness.py — サイレント停滞（silent stall）の再発防止ハードチェック。

data/catalog/indicators.json を読み、各系列について
    age = 今日(JST) − observation_cutoff
が freshness_sla_days の 2 倍を超える系列を「停滞（stale）」として列挙する。
ただし KNOWN_STALE allowlist に載る「更新が来ないのが正常」な系列は対象外。
停滞が 1 件でもあれば exit 1、無ければ exit 0。

D-020②（2026-08-25）: age は observation_cutoff をそのまま引かず、
scripts.common.metadata.effective_cutoff_age() で測る。cutoff_semantics="delivery"
の系列（容量市場 20 + JEPX スポット 10）は cutoff が受渡日 / 受渡年度開始日であり、
とくに容量市場は 2029-04-01 固定 = age が約 −950 日の負値。閾値を永久に超えず
鮮度監視が沈黙していた（＝軸 1 の P1）。実効観測日 = cutoff − delivery_horizon_days
に置き換えることでこの永久沈黙が解消され、公表遅延は grace_days で吸収する。

背景（なぜ 2× なのか）:
    generate_catalog.py は 1×SLA で soft warning を出すが、warning 止まりのため
    ラン自体は緑のまま流れ、見落とされる（＝サイレント停滞。今回の Pink Sheet 2026
    URL 未更新による燃料 8 系列の 7 ヶ月停止がまさにこれ。しかも当時は原因を publication
    lag と誤診して SLA 自体を緩め、警告を黙らせてしまっていた）。
    本スクリプトは「1×=要注意」より一段厳しい 2×SLA を「もう明らかに壊れている」
    ハード閾値とし、nightly の commit & push 後に continue-on-error なしで走らせる。
    データ commit は先に完了しているので、失敗してもデータは着地しつつ、ラン結果が
    赤くなって人間が気付ける（loud failure）設計。
    例: wb-pink-sheet は SLA=90 日 → 2×90=180 日。今回型の停止は約 6 ヶ月で赤くなる。

allowlist（KNOWN_STALE）:
    構造的に更新が来ない系列（例: Brexit で EU ETS を離脱した英国、降雪が稀な地点の
    最深積雪）は、閾値を超えても「異常」ではない。これらを一律に赤くすると gate が
    万年赤 = 赤疲れで無視される（＝サイレント停滞と同じ失敗モード）ため allowlist で除外する。
    逆に allowlist に無い系列が閾値超過したら「予期しない停滞」= 本当に気付くべきサイン。

使い方:
    python scripts/check_staleness.py            # 通常（違反あれば exit 1、無ければ exit 0）
    python scripts/check_staleness.py --list     # 違反一覧のみを 1 行 1 系列で出力
    python scripts/check_staleness.py --catalog PATH       # 対象カタログを差し替え（テスト用）
    python scripts/check_staleness.py --multiplier N       # 閾値倍率（既定 2）

freshness_sla_days の解決:
    catalog の各エントリには generate_catalog.py が source_map.yaml 由来の
    freshness_sla_days を注入済み。欠落していた場合は frequency 別デフォルト
    （scripts/common/metadata.DEFAULT_FRESHNESS_SLA_DAYS）にフォールバックする。

D-020④(c)（2026-08-30）: 軸2（パイプライン生存監視）のレポートを末尾に追加した。
    軸1（上記の observation_cutoff ベース）は「データが古い」を測るのに対し、
    軸2は update_schedule と updated_at から「workflow がもう回っていない」を測る
    別軸（D-020 §2.2）。データ頻度は軸1 の担当で、軸2 は一切見ない。
    ⚠️ 軸2 は **report-only**。判定結果を exit コードに反映しない（gating しない）。
    ④(a)(b) で updated_at を生存信号化した直後であり、まず 1 週間の無事故運転を
    確認してから hard 化する（D-020⑤）。soft 先行にするのは、導入直後の誤検知で
    nightly が万年赤 → 赤疲れ → 無視、という Pink Sheet 型の失敗を避けるため。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.metadata import (  # noqa: E402
    DEFAULT_FRESHNESS_SLA_DAYS,
    axis2_violation,
    effective_cutoff_age,
)

DEFAULT_CATALOG = ROOT / "data" / "catalog" / "indicators.json"
STALE_MULTIPLIER = 2  # freshness_sla_days の何倍で「停滞」と判定するか（1×=soft warning より厳しく）

# 既知の「停滞は許容」系列（allowlist）。id -> 理由（+登録日）。
# ここに載る系列は staleness check の対象外（構造的に更新が来ない = 停滞が異常ではない）。
# 追加するときは必ず「なぜ許容か」と「登録日」をコメント/理由文字列に残すこと。
# 将来この系列が復活・更新再開したら、ここから外して通常監視に戻す。
KNOWN_STALE: dict[str, str] = {
    # --- EU ETS 英国: Brexit で EU ETS を離脱、検証排出データは 2020 年で終端（構造的死系列） ---
    "eu-ets-emissions-country-gb": "UK left EU ETS post-Brexit; data ends 2020 (registered 2026-07-18)",
    "eu-ets-allowances-allocated-country-gb": "UK left EU ETS post-Brexit; data ends 2020 (registered 2026-07-18)",
    # --- EU ETS リヒテンシュタイン: 近年の検証排出データが存在しない（構造的死系列） ---
    "eu-ets-emissions-country-li": "Liechtenstein has no recent verified-emissions data (registered 2026-07-18)",
    "eu-ets-allowances-allocated-country-li": "Liechtenstein has no recent verified-emissions data (registered 2026-07-18)",
    # --- EPRX 需給調整 電源種別別 水力/揚水: FY2025 の年次取りまとめ PDF (2026-06-18 公表) から
    #     EPRX が水力と揚水を「水力・揚水」1 行に合算して公表する方式に変わり、分離値が公表されなくなった。
    #     よって本 11 系列は FY2024 で構造的に終端（更新が来ないのが正常）。
    #     FY2025 以降は balancing-price-{商品}-hydro-pumped が後継。
    #     もし EPRX が分離公表を再開したら、ここから外して通常監視に戻すこと。 ---
    "balancing-price-primary-hydro": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-primary-pumped": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-secondary-1-hydro": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-secondary-1-pumped": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-secondary-2-hydro": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-secondary-2-pumped": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-tertiary-1-hydro": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-tertiary-1-pumped": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-composite-hydro": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-composite-pumped": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    "balancing-price-tertiary-2-pumped": "EPRX merged hydro+pumped from FY2025; series ends FY2024 (registered 2026-08-24)",
    # --- JMA 最深積雪: 積雪が稀な地点。降雪イベントが無い＝値が更新されないのが正常（SLA は既に 365 に緩和済み） ---
    "jma-snow-max-kansai": "seasonal: no snowfall since 2021-01; absence is expected (registered 2026-07-18)",
    "jma-snow-max-shikoku": "seasonal: no snowfall since 2022-02; absence is expected (registered 2026-07-18)",
}


def _now_jst() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))


def resolve_sla(entry: dict) -> int:
    """catalog エントリから freshness_sla_days を解決（欠落時は frequency デフォルト）。"""
    v = entry.get("freshness_sla_days")
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    freq = entry.get("frequency") or "daily"
    return DEFAULT_FRESHNESS_SLA_DAYS.get(freq, 3)


def find_stale(indicators: list[dict], multiplier: int, today) -> tuple[list[dict], int]:
    """
    age > freshness_sla_days × multiplier を満たす系列を列挙（KNOWN_STALE は除外）。
    戻り値は (停滞系列リスト[age 降順], allowlist で除外した停滞件数)。
    observation_cutoff が無い / 不正な系列は age を測れないので対象外（skip）。
    """
    stale: list[dict] = []
    allowlisted_hits = 0
    for entry in indicators:
        cutoff = entry.get("observation_cutoff")
        # D-020②: delivery 系列は cutoff − delivery_horizon_days が実効観測日。
        # cutoff 欠落 / parse 不能は None が返るので従来どおり skip。
        age_days = effective_cutoff_age(entry, today)
        if age_days is None:
            continue
        sla = resolve_sla(entry)
        grace = entry.get("grace_days") or 0
        threshold = sla * multiplier + grace
        if age_days <= threshold:
            continue
        ind_id = entry.get("id", "?")
        if ind_id in KNOWN_STALE:
            # 既知の許容停滞。除外してカウントだけ残す。
            allowlisted_hits += 1
            continue
        stale.append(
            {
                "id": ind_id,
                "domain": entry.get("domain", "?"),
                "frequency": entry.get("frequency", "?"),
                "observation_cutoff": cutoff,
                "age_days": age_days,
                "sla_days": sla,
                "threshold_days": threshold,
                "cutoff_semantics": entry.get("cutoff_semantics") or "observation",
                "delivery_horizon_days": entry.get("delivery_horizon_days"),
                "grace_days": grace,
            }
        )
    stale.sort(key=lambda s: s["age_days"], reverse=True)
    return stale, allowlisted_hits


def format_line(s: dict, multiplier: int) -> str:
    # D-020②: delivery 系列は cutoff がそのままでは age の基準にならないため、
    # 実効値の導出過程（horizon / grace）を行に添えて読み手が再現できるようにする。
    extra = ""
    if s.get("cutoff_semantics") == "delivery":
        extra = (
            f" [semantics=delivery, horizon={s.get('delivery_horizon_days')}d, "
            f"grace={s.get('grace_days')}d]"
        )
    return (
        f"{s['id']}  (domain={s['domain']}, freq={s['frequency']}) "
        f"cutoff={s['observation_cutoff']} age={s['age_days']}d "
        f"> {multiplier}×SLA({s['sla_days']}d)={s['threshold_days']}d{extra}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Staleness hard check (age > 2×SLA → exit 1)")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG,
        help="対象カタログ JSON（既定: data/catalog/indicators.json）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="違反一覧のみを 1 行 1 系列で出力（サマリ行を省く）",
    )
    parser.add_argument(
        "--multiplier",
        type=int,
        default=STALE_MULTIPLIER,
        help=f"閾値倍率（既定 {STALE_MULTIPLIER}）",
    )
    args = parser.parse_args(argv)

    if not args.catalog.exists():
        print(f"ERROR: catalog not found: {args.catalog}", file=sys.stderr)
        return 2
    try:
        catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: failed to parse {args.catalog}: {e}", file=sys.stderr)
        return 2

    indicators = catalog.get("indicators") or []
    now = _now_jst()
    today = now.date()
    stale, allowlisted_hits = find_stale(indicators, args.multiplier, today)

    if args.list:
        # 一覧のみ。停滞ゼロなら何も出さない。
        for s in stale:
            print(format_line(s, args.multiplier))
    else:
        print(
            f"staleness check: catalog={args.catalog.name} "
            f"indicators={len(indicators)} today(JST)={today} "
            f"threshold={args.multiplier}×SLA "
            f"(allowlist={len(KNOWN_STALE)}, allowlisted-stale-skipped={allowlisted_hits})"
        )
        if stale:
            print(f"STALE ({len(stale)} series exceed {args.multiplier}×SLA):")
            for s in stale:
                print(f"  - {format_line(s, args.multiplier)}")
        else:
            print("OK: no unexpected series exceeds the staleness threshold.")

    # --- D-020④(c) 軸2: パイプライン生存監視（report-only） -----------------
    # exit コードには反映しない。ここは「軸1 が沈黙していても workflow の停止だけは
    # 見えている」状態を可視化するための出力。hard 化は D-020⑤。
    axis2_hits = []
    for entry in indicators:
        v = axis2_violation(entry, now)
        if v:
            axis2_hits.append((entry.get("id", "?"), v))
    if not args.list:
        if axis2_hits:
            print(f"AXIS2 (report-only, not gating): {len(axis2_hits)} series")
            for ind_id, reason in axis2_hits:
                print(f"  - {ind_id}: {reason}")
        else:
            print("AXIS2 (report-only, not gating): 0 series")

    if stale:
        # 停滞ありは exit 1（nightly ではデータ commit 後に走るので、ランが赤くなる）。
        # --list は「違反一覧のみ表示」なのでサマリ行は出さず、exit code だけで結果を伝える。
        if not args.list:
            print(
                f"FAIL: {len(stale)} unexpected stale series (age > {args.multiplier}×SLA).",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
