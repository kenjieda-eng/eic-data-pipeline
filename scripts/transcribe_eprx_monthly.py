#!/usr/bin/env python3
"""
scripts/transcribe_eprx_monthly.py — EPRX 需給調整市場 取引実績の取りまとめ結果（年次 PDF）から、
商品別の **月次平均落札単価（全電源・蓄電池）** と **ΔkW 上限価格** を転記する（D-018 / D-020 §8、2026-10-04）。

既存の eprx-balancing 46 系列（年次）は「平均落札単価（年平均）」列と「電源種別別 月次表の単純平均」を
手動転記したもの。本スクリプトは同じ 2 本の PDF から、年次の**もとになっている月次表**をそのまま収載する。
bess-net（Y-09 (B)、R-24 §4、Y-27 §4）が蓄電池の月次分布を使うための系列。

■ 一次資料（両年度とも同じレイアウト。ページは PDF の印字ページ番号）
    「2024年度の取引実績について」（2025-06-19 公表、summary_2024.pdf）
    「2025年度の取引実績について」（2026-06-18 公表、summary_2025.pdf）
    - 商品別「平均落札単価の推移（日平均）」ページの月次表（行「平均単価」/「2025年度」、右端は 計／年平均）
        一次 p.11 ／ 二次① p.19 ／ 二次② p.27 ／ 三次① p.35 ／ 複合 p.43 ／ 三次② p.51
    - 商品別「電源種別別の平均落札単価」ページの月次表（行「蓄電池」。「ー」= 落札なし）
        一次 p.14 ／ 二次① p.22 ／ 二次② p.30 ／ 三次① p.38 ／ 複合 p.46 ／ 三次② p.54
    - 商品別「落札単価の分布」ページ下部の「上限価格 [円/ΔkW・30分]」表（複合商品 / 単独商品）
        一次 p.13 ／ 二次① p.21 ／ 二次② p.29 ／ 三次① p.37 ／ 複合 p.45。三次② のページには上限価格の表が無い。
        2025 年度版は「()内は3月14日適用開始」として改定後の値を併記: 複合・一次・二次① 19.51 → 15.00、
        二次②・三次① 7.21 → 7.21（変更なし）。
    「需給調整市場のΔkW上限価格について」（2026-07-30 公表。上限価格ページ https://www.eprx.or.jp/information/post.php に
    掲載された PDF、cap_20260730.pdf として --pdf-dir に置けば sha256 を照合）
    - 表の該当行「2026年07月30日 2026年08月31日 2026年09月01日 当面の間 10.00 10.00 10.00 7.21 7.21 上限無し」:
      2026 年 9 月 1 日（※1 適用開始日は実需給日）から 複合・一次・二次① の上限を 15.00 → 10.00。二次②・三次① は 7.21 で
      据え置き（※5）、三次② は上限無し。年次 PDF（2026 年度版、2027 年 6 月公表見込み）に載るまでの一次資料。
      CAP_NOTICES に転記し、適用開始日に点を置く。行の source_url はこの上限価格ページ（年次取りまとめページではなく）。
    2025 年度版は 2024 年度の月次行も再掲しており（「2024年度」行）、2024 年度版の値と全 72 値が一致する
    （恒等式③）。

■ 恒等式（LTDC と同じく、全部通らなければ CSV を書かない）
    ① 全電源: 月次表右端の 計／年平均 == 年次系列 balancing-price-{product} の値（公表値どうし、完全一致）
       ※ 年平均は約定量加重なので「月次 12 値の単純平均」とは一致しない。比較するのは公表の年平均列。
    ② 蓄電池: 約定月（「ー」を除く）の単純平均を小数 2 桁に四捨五入 == 年次系列 balancing-price-{product}-battery
       （年次側の転記規約そのもの。FY2024 12/8/11/11/12/11 か月・FY2025 全 12 か月）
    ③ 2025 年度版に再掲された 2024 年度の月次行 == 2024 年度版の月次行（6 商品 × 13 値）
    ④ 上限価格: 2025 年度版の括弧なし値 == 2024 年度版の値（年度初めの上限は両年度同じ）
    ⑤ 公表資料の上限改定（CAP_NOTICES）: 対象商品が年次 PDF の上限表にあり、適用日が年次 PDF の最終点（2026-03-14）より後

■ 一次資料の同一性
    EPRX は URL 不変のまま PDF を in-place で差し替える（source_map の注記）。--pdf-dir に PDF があれば
    sha256 を突き合わせ、違えば書き出さない。PDF が無ければ WARN して進む（恒等式が検算の本体。
    PDF は EPRX の規約上リポジトリに置かない）。

■ 収載しないもの
    - 火力・水力・揚水・VPP の月次（必要になったら同じ表から足せる）
    - 三次② の上限価格（PDF に表が無い）／ FY2023 以前の上限価格（本 PDF に無い）
    - 不足率の月次（年次側と同じく定義の不連続があるため見送り）

■ 値の扱い
    公表値をそのまま（円/ΔkW・30分、小数 2 桁）。蓄電池の「ー」（落札なし）は null（missing_policy: "null"）。
    上限価格は「適用開始日」に点を置く（年度初め 4/1 と改定日。年次 PDF 未収載の改定は公表資料 CAP_NOTICES から）。

使い方:
    python scripts/transcribe_eprx_monthly.py                      # 検算 → data/processed/eprx-balancing/ に書き出し
    python scripts/transcribe_eprx_monthly.py --check-only         # 検算のみ
    python scripts/transcribe_eprx_monthly.py --pdf-dir ../_inbox/eprx   # PDF の sha256 も照合（既定 data/raw/eprx-balancing）
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

SOURCE_KEY = "eprx-balancing"
SOURCE_MAP_PATH = ROOT / "docs" / "source_map.yaml"
DEFAULT_PDF_DIR = ROOT / "data" / "raw" / SOURCE_KEY
DEFAULT_PROCESSED_DIR = ROOT / "data" / "processed" / SOURCE_KEY
# 行の source_url は年次系列と同じく一覧ページ（EPRX への深いリンクを避ける運用。年度別 PDF は SOURCES に記録）
SOURCE_URL = "https://www.eprx.or.jp/information/summary.php"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("transcribe_eprx_monthly")

# --- 一次資料（年度 → ファイル名 / URL / sha256 / 公表日 / 資料名） --------------------------
SOURCES: dict[int, dict[str, str]] = {
    2024: {
        "file": "summary_2024.pdf",
        "url": "https://www.eprx.or.jp/information/docs/summary_2024.pdf",
        "sha256": "98a0291128c3ff90901fe599fce6d71b377ea73533d796c8639603aeb7d1276f",
        "published": "2025-06-19",
        "title": "2024年度の取引実績について",
    },
    2025: {
        "file": "summary_2025.pdf",
        "url": "https://www.eprx.or.jp/information/summary_2025.pdf",
        "sha256": "af43178612d3c4ebe5e6c4aee70238bff6847eacc217544705a0e2bc197572ba",
        "published": "2026-06-18",
        "title": "2025年度の取引実績について",
    },
}

# 商品の順序は PDF の章立て（2-1 一次 … 2-6 三次②）
PRODUCTS: list[tuple[str, str]] = [
    ("primary", "一次調整力"),
    ("secondary-1", "二次調整力①"),
    ("secondary-2", "二次調整力②"),
    ("tertiary-1", "三次調整力①"),
    ("composite", "複合商品"),
    ("tertiary-2", "三次調整力②"),
]
PAGE_OVERALL = {"primary": 11, "secondary-1": 19, "secondary-2": 27, "tertiary-1": 35, "composite": 43, "tertiary-2": 51}
PAGE_BATTERY = {"primary": 14, "secondary-1": 22, "secondary-2": 30, "tertiary-1": 38, "composite": 46, "tertiary-2": 54}
PAGE_CAP = {"primary": 13, "secondary-1": 21, "secondary-2": 29, "tertiary-1": 37, "composite": 45}

# --- 月次平均落札単価（全電源、円/ΔkW・30分）: 4月 … 3月 の 12 値 + 右端の 計／年平均 --------------
# 2024: 各商品「平均落札単価の推移」ページの行「平均単価」。2025: 同ページの行「2025年度」。
MONTHLY_OVERALL: dict[int, dict[str, list[float]]] = {
    2024: {
        "primary":     [2.19, 3.19, 2.65, 2.71, 3.72, 3.03, 3.48, 3.85, 3.09, 2.97, 3.17, 3.26, 3.10],
        "secondary-1": [3.17, 2.65, 2.79, 3.40, 3.88, 3.58, 3.74, 3.37, 3.05, 3.11, 2.86, 2.80, 3.21],
        "secondary-2": [2.53, 2.55, 2.52, 2.55, 3.31, 3.00, 3.44, 2.90, 2.34, 2.37, 2.22, 2.50, 2.67],
        "tertiary-1":  [2.54, 2.48, 2.63, 2.55, 3.29, 3.08, 3.21, 2.81, 2.46, 2.42, 2.37, 2.53, 2.69],
        "composite":   [2.67, 2.76, 2.75, 2.66, 3.34, 3.09, 3.34, 3.01, 2.56, 2.51, 2.49, 2.64, 2.80],
        "tertiary-2":  [4.70, 6.03, 2.24, 3.33, 2.65, 4.89, 3.18, 1.18, 1.19, 0.70, 1.08, 1.65, 3.30],
    },
    2025: {
        "primary":     [2.85, 3.53, 3.81, 4.38, 3.76, 3.40, 3.33, 4.03, 3.98, 4.47, 4.77, 4.31, 3.93],
        "secondary-1": [2.40, 2.85, 3.16, 3.36, 3.06, 2.98, 2.78, 2.73, 2.75, 2.66, 2.55, 2.48, 2.79],
        "secondary-2": [2.29, 2.59, 2.51, 3.43, 2.87, 2.67, 2.46, 2.40, 2.64, 2.28, 2.14, 2.24, 2.53],
        "tertiary-1":  [2.24, 2.65, 2.51, 3.43, 2.80, 2.66, 2.40, 2.33, 2.43, 2.10, 2.00, 2.18, 2.47],
        "composite":   [2.39, 2.82, 2.76, 3.72, 3.07, 2.81, 2.57, 2.72, 3.03, 2.93, 2.86, 2.66, 2.83],
        "tertiary-2":  [1.12, 1.42, 0.99, 0.81, 1.35, 1.13, 0.83, 0.77, 0.59, 0.93, 1.10, 1.97, 1.15],
    },
}
# 2025 年度版に再掲された「2024年度」行（恒等式③の比較対象。2024 年度版の行と独立に転記）
OVERALL_FY2024_REPRINTED_IN_FY2025: dict[str, list[float]] = {
    "primary":     [2.19, 3.19, 2.65, 2.71, 3.72, 3.03, 3.48, 3.85, 3.09, 2.97, 3.17, 3.26, 3.10],
    "secondary-1": [3.17, 2.65, 2.79, 3.40, 3.88, 3.58, 3.74, 3.37, 3.05, 3.11, 2.86, 2.80, 3.21],
    "secondary-2": [2.53, 2.55, 2.52, 2.55, 3.31, 3.00, 3.44, 2.90, 2.34, 2.37, 2.22, 2.50, 2.67],
    "tertiary-1":  [2.54, 2.48, 2.63, 2.55, 3.29, 3.08, 3.21, 2.81, 2.46, 2.42, 2.37, 2.53, 2.69],
    "composite":   [2.67, 2.76, 2.75, 2.66, 3.34, 3.09, 3.34, 3.01, 2.56, 2.51, 2.49, 2.64, 2.80],
    "tertiary-2":  [4.70, 6.03, 2.24, 3.33, 2.65, 4.89, 3.18, 1.18, 1.19, 0.70, 1.08, 1.65, 3.30],
}

# --- 電源種別別 月次平均落札単価の「蓄電池」行（円/ΔkW・30分）。None = 「ー」（落札なし） ----------
MONTHLY_BATTERY: dict[int, dict[str, list[float | None]]] = {
    2024: {
        "primary":     [16.28, 18.55, 17.77, 15.52, 17.28, 14.36, 18.33, 18.19, 17.64, 14.78, 11.42, 11.76],
        "secondary-1": [3.58, 4.11, 9.00, None, None, None, None, 19.01, 14.11, 5.52, 3.24, 3.08],
        "secondary-2": [7.00, 8.77, 19.29, 19.39, 19.50, 19.50, None, 19.01, 14.13, 5.52, 3.24, 3.36],
        "tertiary-1":  [7.00, None, 18.94, 15.78, 15.88, 8.52, 6.17, 14.62, 13.94, 6.12, 4.50, 5.15],
        "composite":   [16.16, 18.46, 17.76, 15.34, 17.05, 13.31, 18.15, 18.03, 17.53, 14.69, 11.39, 11.74],
        "tertiary-2":  [234.89, 200.39, 177.70, 171.50, 164.55, 82.19, 84.69, 17.11, None, 9.81, 17.95, 43.00],
    },
    2025: {
        "primary":     [10.85, 10.27, 13.48, 13.52, 10.73, 9.61, 8.82, 11.55, 12.26, 13.49, 13.24, 10.39],
        "secondary-1": [10.78, 7.43, 18.72, 17.33, 18.32, 12.22, 15.71, 9.14, 11.61, 10.67, 7.84, 10.32],
        "secondary-2": [10.64, 8.14, 18.77, 17.21, 18.22, 13.03, 15.49, 8.15, 13.16, 10.59, 8.63, 11.69],
        "tertiary-1":  [10.75, 7.91, 18.15, 15.96, 17.78, 12.40, 13.86, 7.86, 12.68, 10.39, 8.38, 11.22],
        "composite":   [10.83, 10.25, 13.45, 13.49, 10.72, 9.59, 8.82, 11.54, 12.26, 13.48, 13.22, 10.38],
        "tertiary-2":  [42.28, 5.76, 3.52, 50.83, 58.28, 40.42, 1.58, 0.92, 5.73, 2.64, 9.66, 10.05],
    },
}

# --- ΔkW 上限価格（円/ΔkW・30分）。単独応札に適用される商品ごとの上限（複合応札には複合商品の上限） ---
# 各年度版「落札単価の分布」ページの表。2025 年度版は「()内は3月14日適用開始」で改定後を併記。
CAP_BY_EDITION: dict[int, dict[str, tuple[float, float | None]]] = {
    # product: (年度初めの上限, 2026-03-14 適用開始の上限 or None)
    2024: {"composite": (19.51, None), "primary": (19.51, None), "secondary-1": (19.51, None),
           "secondary-2": (7.21, None), "tertiary-1": (7.21, None)},
    2025: {"composite": (19.51, 15.00), "primary": (19.51, 15.00), "secondary-1": (19.51, 15.00),
           "secondary-2": (7.21, 7.21), "tertiary-1": (7.21, 7.21)},
}
CAP_REVISION_DATE = "2026-03-14"

# --- 年次 PDF 以外の公表資料による上限価格の改定（年次 PDF に載るまでの一次資料。適用開始日に点を置く）-------
# 「需給調整市場のΔkW上限価格について」（2026-07-30 公表、https://www.eprx.or.jp/information/post.php）:
#   2026 年 9 月 1 日実需給分から 複合・一次・二次① の上限を 15.00 → 10.00。二次②・三次① は 7.21 のまま（点を足さない）。
#   年次 PDF（2026 年度版）が同じ値を載せたら、CAP_BY_EDITION 側に移してここから外す（点は同じなので CSV は変わらない）。
CAP_NOTICES: list[dict] = [
    {
        "effective": "2026-09-01",
        "published": "2026-07-30",
        "title": "需給調整市場のΔkW上限価格について",
        "url": "https://www.eprx.or.jp/information/post.php",  # 行の source_url（上限価格ページ）
        "pdf_file": "cap_20260730.pdf",  # --pdf-dir に置いたときの名前（リポには置かない）
        "pdf_url": "https://www.eprx.or.jp/information/docs/14b85fbe8942da7d3bdddda80243e7e170f64d9e.pdf",
        "pdf_sha256": "06f84fe90ea852335c12ca35db613f700e36f72051953de758d5046471431d4a",  # 217,685 bytes、2026-10-08 実機確認
        "values": {"composite": 10.00, "primary": 10.00, "secondary-1": 10.00},
    },
]


def month_dates(fy: int) -> list[str]:
    """年度 fy の 4 月〜翌 3 月の月初日。"""
    return [f"{fy}-{m:02d}-01" for m in range(4, 13)] + [f"{fy + 1}-{m:02d}-01" for m in range(1, 4)]


def r2(x: float) -> float:
    return round(x + 1e-9, 2)


def verify_pdfs(pdf_dir: Path) -> tuple[list[str], list[str]]:
    """PDF があれば sha256 を照合。戻り値は (問題, 情報)。PDF が無いのは問題ではなく情報（WARN）。"""
    problems, infos = [], []
    for fy, s in sorted(SOURCES.items()):
        p = pdf_dir / s["file"]
        if not p.exists():
            infos.append(f"{fy}: PDF not present at {p} — sha256 未照合（恒等式のみで検算）")
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest != s["sha256"]:
            problems.append(f"{fy}: PDF sha256 mismatch: {digest} != {s['sha256']} — EPRX が PDF を差し替えた可能性。表を読み直すまで書き出さない")
        else:
            infos.append(f"{fy}: PDF sha256 一致（{s['file']}）")
    for n in CAP_NOTICES:  # 公表資料の PDF も同じ扱い（あれば照合、無ければ WARN）
        p = pdf_dir / n["pdf_file"]
        if not p.exists():
            infos.append(f"{n['effective']}: PDF not present at {p} — sha256 未照合（恒等式のみで検算）")
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest != n["pdf_sha256"]:
            problems.append(f"{n['effective']}: PDF sha256 mismatch: {digest} != {n['pdf_sha256']} — EPRX が PDF を差し替えた可能性。表を読み直すまで書き出さない")
        else:
            infos.append(f"{n['effective']}: PDF sha256 一致（{n['pdf_file']}）")
    return problems, infos


def annual_value(processed_dir: Path, indicator_id: str, fy: int) -> float | None:
    """年次系列（既収載）の FY 値を読む。無ければ None。"""
    p = processed_dir / f"{indicator_id}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype={"date": str}, float_precision="round_trip")
    row = df[df["date"] == f"{fy}-04-01"]
    if row.empty:
        return None
    return float(row["value"].iloc[0])


def check_identities(processed_dir: Path) -> tuple[list[str], list[str]]:
    ok, ng = [], []

    def rec(name: str, good: bool, detail: str) -> None:
        (ok if good else ng).append(f"{name}: {detail}")

    for fy in sorted(MONTHLY_OVERALL):
        for pid, _jp in PRODUCTS:
            row = MONTHLY_OVERALL[fy][pid]
            if len(row) != 13:
                rec(f"FY{fy} {pid} 全電源 行長", False, f"{len(row)} != 13")
                continue
            # ① 公表の年平均（右端）== 年次系列（公表値どうし。差 0 を要求）
            got, exp = row[12], annual_value(processed_dir, f"balancing-price-{pid}", fy)
            rec(f"① FY{fy} {pid} 年平均列 = 年次系列", exp is not None and abs(got - exp) < 1e-9,
                f"{got} vs {exp}")
            # ② 蓄電池: 約定月の単純平均を 2 桁丸め == 年次 battery 系列（転記規約どおり）
            b = MONTHLY_BATTERY[fy][pid]
            if len(b) != 12:
                rec(f"FY{fy} {pid} 蓄電池 行長", False, f"{len(b)} != 12")
                continue
            vals = [v for v in b if v is not None]
            mean = r2(sum(vals) / len(vals))
            exp_b = annual_value(processed_dir, f"balancing-price-{pid}-battery", fy)
            rec(f"② FY{fy} {pid} 蓄電池 月次単純平均(2桁) = 年次系列", exp_b is not None and abs(mean - exp_b) < 1e-9,
                f"n={len(vals)} mean={sum(vals) / len(vals):.4f} → {mean} vs {exp_b}")
    # ③ 2025 年度版に再掲された 2024 年度行 == 2024 年度版
    for pid, _jp in PRODUCTS:
        a, b = MONTHLY_OVERALL[2024][pid], OVERALL_FY2024_REPRINTED_IN_FY2025[pid]
        rec(f"③ {pid} 2024年度行（2025年度版の再掲）= 2024年度版", a == b, "一致" if a == b else f"{a} != {b}")
    # ④ 上限価格: 2025 年度版の括弧なし値 == 2024 年度版の値
    for pid in CAP_BY_EDITION[2024]:
        a, b = CAP_BY_EDITION[2024][pid][0], CAP_BY_EDITION[2025][pid][0]
        rec(f"④ {pid} 上限価格 年度初め 2024 = 2025", abs(a - b) < 1e-9, f"{a} vs {b}")
    # ⑤ 公表資料の改定: 対象商品が年次 PDF の上限表にあり、適用日が年次 PDF の最終点より後（年次 PDF と二重に点を置かない）
    for n in CAP_NOTICES:
        for pid, v in n["values"].items():
            rec(f"⑤ {pid} 上限価格 公表資料 {n['effective']} = {v}",
                pid in CAP_BY_EDITION[2025] and n["effective"] > CAP_REVISION_DATE,
                f"{n['title']}（{n['published']}）")
    return ok, ng


def build_rows() -> list[dict]:
    rows: list[dict] = []

    def add(date: str, ind: str, value, source_url: str = SOURCE_URL) -> None:
        rows.append({"date": date, "indicator_id": ind, "region": "jp", "value": value, "source_url": source_url})

    for fy in sorted(MONTHLY_OVERALL):
        dates = month_dates(fy)
        for pid, _jp in PRODUCTS:
            for d, v in zip(dates, MONTHLY_OVERALL[fy][pid][:12]):
                add(d, f"balancing-price-monthly-{pid}", v)
            for d, v in zip(dates, MONTHLY_BATTERY[fy][pid]):
                add(d, f"balancing-price-monthly-{pid}-battery", v)  # None → null
    for pid in CAP_BY_EDITION[2024]:
        add("2024-04-01", f"balancing-price-cap-{pid}", CAP_BY_EDITION[2024][pid][0])
        add("2025-04-01", f"balancing-price-cap-{pid}", CAP_BY_EDITION[2025][pid][0])
        rev = CAP_BY_EDITION[2025][pid][1]
        if rev is not None:
            add(CAP_REVISION_DATE, f"balancing-price-cap-{pid}", rev)
    for n in CAP_NOTICES:
        for pid, v in n["values"].items():
            add(n["effective"], f"balancing-price-cap-{pid}", v, source_url=n["url"])  # 一次資料のページ
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check-only", action="store_true", help="恒等式の検算のみ（書き出さない）")
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR, help="一次資料 PDF の置き場（あれば sha256 を照合）")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED_DIR, help="書き出し先（年次系列の読み元でもある）")
    args = parser.parse_args(argv)

    # 1) 一次資料の同一性（PDF があれば）
    problems, infos = verify_pdfs(args.pdf_dir)
    for line in infos:
        (logger.warning if "未照合" in line else logger.info)("PDF: %s", line)
    for line in problems:
        logger.error("PDF: %s", line)
    if problems:
        logger.error("FAIL: 一次資料の照合に失敗したため書き出さない（%d 件）", len(problems))
        return 1

    # 2) 恒等式
    ok, ng = check_identities(args.processed_dir)
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
    n_null = sum(1 for r in rows if r["value"] is None)
    logger.info("transcribed: %d rows (%d values + %d null) / %d series", len(rows), len(rows) - n_null, n_null, len(ids))
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
    listed = set(cfg.get("indicator_ids") or [])
    missing = [i for i in ids if i not in declared or i not in listed]
    if missing:
        logger.error("source_map の indicators / indicator_ids に無い id: %s", missing)
        return 1

    df_all = pd.DataFrame(rows)
    for ind in ids:
        df = df_all[df_all["indicator_id"] == ind][["date", "indicator_id", "region", "value", "source_url"]].reset_index(drop=True)
        write_processed(df, args.processed_dir, ind, replace=True)
        write_metadata_for_indicator(args.processed_dir, cfg, ind, df)
    logger.info("OK: wrote %d series to %s", len(ids), args.processed_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
