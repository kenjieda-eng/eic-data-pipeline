#!/usr/bin/env python3
"""
scripts/transcribe_ltdc.py — 長期脱炭素電源オークション（LTDC）約定結果の手動転記（D-020 §8）。

OCCTO は LTDC の約定結果を **PDF のスライド**でしか公表しない（CSV/API なし）。数値は
p.10 の表と p.14/p.15 のグラフの**値ラベル**にあり、機械可読な配布物が存在しないため、
fetch_*.py（自動取得）ではなく本スクリプトによる**手動転記 + 機械検算**の形を取る。
EPRX 年次取りまとめ（D-018）と同じ運用方針。

■ なぜ「転記」を人がやり、「検算」を機械がやるのか
  転記は 2 度事故を起こしている（D-020 §8.2）:
    (1) 億円の桁を 10 分の 1 に取り違えた（4,748 → 474.8）
    (2) 公表本文が「応札容量（落札率）」と書きながら**落札容量**を列挙していた回（応札年度2025）を
        そのまま応札容量として読んだ
  → 値を打ち込んだあと、**恒等式が全部通らなければ CSV を書かない**。
     ①Σ電源種別 = 全国 ②Σエリア = 全国 ③脱炭素 + LNG = 全国 ④図の LNG = 表の LNG
     ⑤落札率 = 落札 ÷ 応札 ⑥本文の集約値 = 図の内訳の和
  転記の優先順位は **p.10 の表 > 図の値ラベル > 本文**（本文は上記 (2) の実績があるため最後）。

■ 一次資料の同一性
  OCCTO は EPRX と同様に「URL 不変のまま PDF を差し替える」ことがある（D-020 §9 の EPRX 実績）。
  本スクリプトは data/raw/occto-ltdc/*.pdf の **sha256 を突き合わせてから**転記表を採用する。
  ハッシュが変わっていたら図を読み直すまで書き出さない（= 差し替えを黙って通さない）。

■ 収載しないもの
  - 応札容量の「逆算」（落札 ÷ 落札率）: 落札率が整数％丸めのため ±1% の幅が出る。図に応札の
    値ラベルがあるので一次値を使う（D-020 §8.3 の基準: 丸めた値を分母にする導出は catalog に置かない）
  - LNG の募集量: 応札年度2023 は「2023〜2025 の 3 年間の募集量 600万kW」、2024・2025 は各回の
    残枠。回をまたいで意味が変わるため系列にしない（notes に残す）

■ 単位
  公表は 万kW / 億円/年。catalog は kW（万kW ×10,000）/ **億円/年（公表のまま）** / %（公表の整数）。
  約定総額を円に展開しないのは、1 円単位の精度を騙らないため（EDINET の百万円と同じ流儀）。

使い方:
    python scripts/transcribe_ltdc.py                 # 検算 → data/processed/occto-ltdc/ に書き出し
    python scripts/transcribe_ltdc.py --check-only    # 検算のみ（書き出さない）
    python scripts/transcribe_ltdc.py --raw-dir PATH  # PDF の置き場を差し替え（既定 data/raw/occto-ltdc）
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.io import write_processed  # noqa: E402
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

SOURCE_KEY = "occto-ltdc"
SOURCE_MAP_PATH = ROOT / "docs" / "source_map.yaml"
DEFAULT_RAW_DIR = ROOT / "data" / "raw" / SOURCE_KEY
DEFAULT_PROCESSED_DIR = ROOT / "data" / "processed" / SOURCE_KEY

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("transcribe_ltdc")

# --- 一次資料（応札年度 → ファイル名 / URL / sha256 / 公表日） ---------------------------
SOURCES: dict[int, dict[str, str]] = {
    2023: {
        "file": "longauction_kekka_ousatsu2023.pdf",
        "url": "https://www.occto.or.jp/assets/market-board/market/oshirase/2024/files/240426_longauction_youryouyakujokekka_kouhyou_ousatsu2023.pdf",
        "sha256": "3b75fce18767b48a72d24d239273b05b48bc8e84a302f964acb89c6e60278ca2",
        "published": "2024-04-26",
    },
    2024: {
        "file": "longauction_kekka_ousatsu2024.pdf",
        "url": "https://www.occto.or.jp/assets/market-board/market/oshirase/2025/files/250428_longauction_youryouyakujokekka_kouhyou_ousatsu2024.pdf",
        "sha256": "d1fdeca1de0b5f533d4f19c29e3065e5c77805ce9a5ae7330f5925a8db89f1c8",
        "published": "2025-04-28",
    },
    2025: {
        "file": "longauction_kekka_ousatsu2025.pdf",
        "url": "https://www.occto.or.jp/assets/various/capacity-market/jitsujukyukanren/2025_boshuyoukou_long/260513_longauction_youryouyakujokekka_kouhyou_ousatsu2025.pdf",
        "sha256": "77f2b6996f88a80a1fc90a9a521f4d90e44b7f968351e833629933efd137b1d6",
        "published": "2026-05-13",
    },
}

# --- p.14「発電方式別の応札容量・落札容量」の値ラベル（万kW）: カテゴリ → [(応札, 落札), ...] ----
# 内訳の分け方は回ごとに変わる（揚水 3h/6h・新設/リプレース、蓄電池 Li/Li 以外 など）ため、
# 回をまたいで意味が安定する 8 区分に集約して持つ。内訳は notes に残す。
# 図に現れない区分は 0（全バーの合計が全国応札容量と一致するので、未掲載区分の応札は 0 と確定できる）。
BY_SOURCE: dict[int, dict[str, list[tuple[float, float]]]] = {
    2023: {  # 第 1 回: 揚水 / 蓄電池 / 水素混焼改修・アンモニア混焼改修・水素混焼(リプレース) / バイオマス専焼 / 原子力 / その他 / LNG
        "pumped": [(83.8, 57.7)],
        "battery": [(455.9, 109.2)],
        "thermal-decarbon": [(5.5, 5.5), (77.0, 77.0), (6.8, 0.0)],
        "biomass": [(19.9, 19.9)],
        "nuclear-new": [(131.6, 131.6)],
        "nuclear-existing-safety": [],  # 制度対象は応札年度2024から
        "hydro-general": [],            # 図の「その他・水力・太陽光・風力」に含まれ 0
        "other": [(0.0, 0.0)],
        "lng": [(575.6, 575.6)],
    },
    2024: {  # 第 2 回: 揚水 3h~6h / 6h~ ・蓄電池 3h~6h / 6h~ ・アンモニア混焼改修・既設原子力安全対策・一般水力・その他・LNG
        "pumped": [(9.8, 0.0), (75.6, 36.1)],
        "battery": [(514.0, 96.1), (181.6, 40.9)],
        "thermal-decarbon": [(9.5, 9.5)],
        "biomass": [],
        "nuclear-new": [],
        "nuclear-existing-safety": [(434.8, 315.3)],
        "hydro-general": [(5.2, 5.2)],
        "other": [(0.0, 0.0)],
        "lng": [(131.5, 131.5)],
    },
    2025: {  # 第 3 回: 揚水 新設/リプレース等・蓄電池 Li/Li 以外・アンモニア混焼改修・水素専焼・バイオマス専焼・原子力・既設原子力安全対策・その他・LNG
        "pumped": [(18.6, 18.6), (63.2, 26.8)],
        "battery": [(152.5, 55.1), (120.6, 70.0)],
        "thermal-decarbon": [(26.4, 26.4), (25.3, 25.3)],
        "biomass": [(10.1, 10.1)],
        "nuclear-new": [(138.1, 138.1)],
        "nuclear-existing-safety": [(55.8, 55.8)],
        "hydro-general": [],
        "other": [(0.0, 0.0)],
        "lng": [(475.0, 303.8)],
    },
}

# --- p.10 の表（万kW / 億円/年） -----------------------------------------------------------
TOTALS: dict[int, dict] = {
    2023: dict(decarbon_awarded=401.0, decarbon_target=400.0, lng_awarded=575.6,
               decarbon_amount=2336, lng_amount=1766, decarbon_net3=706, lng_net3=-1343,
               bid_total=1356.2, awarded_total=976.6, award_rate_total=72),
    2024: dict(decarbon_awarded=503.0, decarbon_target=500.0, lng_awarded=131.5,
               decarbon_amount=3464, lng_amount=456, decarbon_net3=945, lng_net3=-52,
               bid_total=1361.9, awarded_total=634.5, award_rate_total=47),
    2025: dict(decarbon_awarded=426.1, decarbon_target=500.0, lng_awarded=303.8,
               decarbon_amount=4748, lng_amount=1444, decarbon_net3=3420, lng_net3=810,
               bid_total=1085.6, awarded_total=729.9, award_rate_total=67),
}

# --- p.15「エリア別の応札容量・落札容量」の値ラベル（万kW）: (応札, 落札) --------------------
AREAS = ["hokkaido", "tohoku", "tokyo", "chubu", "hokuriku", "kansai", "chugoku", "shikoku", "kyushu"]
BY_AREA: dict[int, list[tuple[float, float]]] = {
    2023: [(222.9, 120.7), (156.3, 78.4), (103.5, 60.5), (221.6, 178.8), (8.1, 4.0),
           (376.2, 324.6), (189.3, 187.7), (6.0, 0.0), (72.4, 21.8)],
    2024: [(236.1, 166.3), (349.0, 56.4), (405.7, 248.1), (49.0, 10.2), (23.2, 9.6),
           (73.8, 36.1), (47.4, 28.7), (81.2, 69.2), (96.5, 9.9)],
    2025: [(106.3, 101.6), (278.1, 196.6), (231.4, 182.2), (4.7, 4.7), (91.0, 75.9),
           (184.4, 13.1), (39.2, 27.2), (21.6, 7.5), (128.8, 121.1)],
}

# --- 本文の集約値（万kW）。図の内訳の和と突き合わせる ----------------------------------------
# ⚠️ 応札年度2025 の本文は「応札容量（落札率）は…」と書きながら**落札容量**を列挙している
#    （恒等式 4 本と図で確定。D-020 §8.2）。列挙されている側を bid / awarded のどちらで
#    検算するかを回ごとに明示する。
TEXT_AGG: dict[int, dict[str, tuple[float | None, float | None]]] = {
    2023: {"battery": (455.9, None), "pumped": (83.8, None)},   # 本文 = 応札容量
    2024: {"battery": (695.6, None), "pumped": (85.4, None)},   # 本文 = 応札容量
    2025: {"battery": (None, 125.1), "pumped": (None, 45.3)},   # 本文 = 落札容量（表記は「応札容量」）
}

TOL = 0.15  # 万kW。内訳の 0.1 万kW 丸めが最大 2 本分ずれても通る幅


def r1(x: float) -> float:
    return round(x + 1e-9, 1)


def verify_raw(raw_dir: Path) -> list[str]:
    """一次資料 PDF の sha256 を突き合わせる。不一致・欠落は違反として返す。"""
    problems = []
    for fy, s in sorted(SOURCES.items()):
        p = raw_dir / s["file"]
        if not p.exists():
            problems.append(f"{fy}: raw PDF not found: {p} （{s['url']} から取得すること）")
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest != s["sha256"]:
            problems.append(
                f"{fy}: raw PDF sha256 mismatch: {digest} != {s['sha256']}"
                " — OCCTO が PDF を差し替えた可能性。図を読み直して転記表を更新するまで書き出さない"
            )
    return problems


def check_identities() -> tuple[list[str], list[str]]:
    """恒等式で転記表を検算する。戻り値は (OK 行, NG 行)。"""
    ok, ng = [], []

    def rec(fy: int, name: str, got, exp) -> None:
        good = abs(got - exp) <= TOL if isinstance(exp, float) else got == exp
        (ok if good else ng).append(f"{fy} {name}: {got} vs {exp}")

    for fy in sorted(BY_SOURCE):
        bs, t = BY_SOURCE[fy], TOTALS[fy]
        bid_sum = r1(sum(p[0] for parts in bs.values() for p in parts))
        awd_sum = r1(sum(p[1] for parts in bs.values() for p in parts))
        ab = r1(sum(a for a, _ in BY_AREA[fy]))
        aa = r1(sum(b for _, b in BY_AREA[fy]))
        rec(fy, "Σ電源種別 応札 = 全国応札", bid_sum, t["bid_total"])
        rec(fy, "Σ電源種別 落札 = 全国落札", awd_sum, t["awarded_total"])
        rec(fy, "Σエリア 応札 = 全国応札", ab, t["bid_total"])
        rec(fy, "Σエリア 落札 = 全国落札", aa, t["awarded_total"])
        rec(fy, "脱炭素落札 + LNG落札 = 全国落札",
            r1(t["decarbon_awarded"] + t["lng_awarded"]), t["awarded_total"])
        rec(fy, "図のLNG落札 = 表のLNG落札", r1(sum(p[1] for p in bs["lng"])), t["lng_awarded"])
        rec(fy, "落札率 = 落札/応札",
            round(100 * t["awarded_total"] / t["bid_total"]), t["award_rate_total"])
        for cat, (tb, ta) in TEXT_AGG[fy].items():
            if tb is not None:
                rec(fy, f"本文 {cat} 応札 = 図の和", r1(sum(p[0] for p in bs[cat])), tb)
            if ta is not None:
                rec(fy, f"本文 {cat} 落札 = 図の和", r1(sum(p[1] for p in bs[cat])), ta)
    return ok, ng


def build_rows() -> list[dict]:
    """検算済みの転記表から共通スキーマの行を作る。"""
    rows: list[dict] = []

    def add(fy: int, ind: str, region: str, value: float) -> None:
        rows.append({
            "date": f"{fy}-04-01", "indicator_id": ind, "region": region,
            "value": value, "source_url": SOURCES[fy]["url"],
        })

    for fy in sorted(BY_SOURCE):
        bs, t = BY_SOURCE[fy], TOTALS[fy]
        for cat, parts in bs.items():
            if cat == "other":   # 「その他」は残差であって電源種ではないので系列にしない
                continue
            add(fy, f"ltdc-bid-{cat}", "jp", round(r1(sum(p[0] for p in parts)) * 10_000))
            add(fy, f"ltdc-awarded-{cat}", "jp", round(r1(sum(p[1] for p in parts)) * 10_000))
        add(fy, "ltdc-bid-total", "jp", round(t["bid_total"] * 10_000))
        add(fy, "ltdc-awarded-total", "jp", round(t["awarded_total"] * 10_000))
        add(fy, "ltdc-awarded-decarbon", "jp", round(t["decarbon_awarded"] * 10_000))
        # ltdc-awarded-lng は上の電源種別ループが出す（恒等式④で図 = 表を確認済み）
        add(fy, "ltdc-target-decarbon", "jp", round(t["decarbon_target"] * 10_000))
        add(fy, "ltdc-award-rate-total", "jp", t["award_rate_total"])
        # 約定総額は公表どおり 億円/年 のまま持つ（円に展開すると 1 円単位の精度を騙ることになる）
        add(fy, "ltdc-contract-amount-decarbon", "jp", t["decarbon_amount"])
        add(fy, "ltdc-contract-amount-lng", "jp", t["lng_amount"])
        add(fy, "ltdc-contract-amount-net-decarbon", "jp", t["decarbon_net3"])
        add(fy, "ltdc-contract-amount-net-lng", "jp", t["lng_net3"])
        for area, (b, a) in zip(AREAS, BY_AREA[fy]):
            add(fy, f"ltdc-bid-area-{area}", area, round(b * 10_000))
            add(fy, f"ltdc-awarded-area-{area}", area, round(a * 10_000))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="恒等式の検算のみ（書き出さない）")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help="一次資料 PDF の置き場")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED_DIR, help="書き出し先")
    args = parser.parse_args(argv)

    # 1) 一次資料の同一性
    problems = verify_raw(args.raw_dir)
    for p in problems:
        logger.error("RAW: %s", p)
    if problems:
        logger.error("FAIL: 一次資料の照合に失敗したため書き出さない（%d 件）", len(problems))
        return 1
    logger.info("raw ok: %d PDFs, sha256 一致", len(SOURCES))

    # 2) 恒等式
    ok, ng = check_identities()
    for line in ok:
        logger.info("OK  %s", line)
    for line in ng:
        logger.error("NG  %s", line)
    logger.info("identities: %d ok / %d ng", len(ok), len(ng))
    if ng:
        logger.error("FAIL: 恒等式が %d 本通らないため書き出さない（転記表を見直すこと）", len(ng))
        return 1

    rows = build_rows()
    ids = sorted({r["indicator_id"] for r in rows})
    logger.info("transcribed: %d rows / %d series / %d rounds", len(rows), len(ids), len(SOURCES))
    if args.check_only:
        logger.info("--check-only: 書き出しは行わない")
        return 0

    # 3) 書き出し（系列ごとに全置換。転記表が唯一の真実なので追記マージはしない）
    with SOURCE_MAP_PATH.open("r", encoding="utf-8") as f:
        cfg = (yaml.safe_load(f) or {}).get("sources", {}).get(SOURCE_KEY)
    if not cfg:
        logger.error("source_map.yaml に sources.%s がない", SOURCE_KEY)
        return 1
    declared = set(cfg.get("indicators") or {})
    if declared != set(ids):
        logger.error("source_map の indicators と転記結果が一致しない: only_map=%s only_rows=%s",
                     sorted(declared - set(ids)), sorted(set(ids) - declared))
        return 1

    df_all = pd.DataFrame(rows)
    for ind in ids:
        df = df_all[df_all["indicator_id"] == ind][
            ["date", "indicator_id", "region", "value", "source_url"]
        ].reset_index(drop=True)
        write_processed(df, args.processed_dir, ind, replace=True)
        write_metadata_for_indicator(args.processed_dir, cfg, ind, df)
    logger.info("OK: wrote %d series to %s", len(ids), args.processed_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
