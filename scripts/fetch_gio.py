"""
日本の温室効果ガス排出量（国立環境研究所 GIO インベントリ）の取得スクリプト
（esg ドメイン、18 系列、1990〜最新年度）。

L-063 事前確認: energy-data-platform/docs/l063-check-energy-gx-2026-08-07.md §S-1
（🟡 条件付き GO。下記 4 つの特殊要件を実装で吸収することが GO の条件）

--------------------------------------------------------------------------
特殊要件 1: ライセンス = gio-terms（新規識別子）
--------------------------------------------------------------------------
GIO サイトポリシー (https://www.nies.go.jp/gio/copyright/index.html) は二層構造で、
サイト全体の一般著作権条項（複製・頒布に事前許諾が必要）とは別に
「温室効果ガス排出量データの利用規約」という独立した節がある。本データにはそちらが
適用され、無改変頒布・派生物の公表・商用利用がすべて明示許諾されている。
ただし頒布物にも同規約が引き継がれる（share-alike 的条項）ため CC BY 4.0 への
再ライセンスは不可 → LICENSE_VALUES に `gio-terms` を新設した。
license_notice には規約上の 3 つの通知義務（原頒布元 URL / 本規約適用 / 随時更新）を
必ず書き込む。実体は docs/source_map.yaml の gio セクション。

--------------------------------------------------------------------------
特殊要件 2: 追記ではなく「毎回ファイル全体を再生成」（全置換）
--------------------------------------------------------------------------
規約に次の条項がある:

    公開日の異なる版を組み合わせての使用は避けてください
    （例 GIOデータの最新版とそれ以前の版を組み合わせて時系列を作成すること）

インベントリは毎年の再計算で **過去年（1990 年度まで遡って）も改定される**ので、
実務上も追記マージは誤り（旧版と新版の値が混ざった時系列になる）。
→ write_processed(..., replace=True) で毎回 1990〜最新年度を丸ごと書き直す。
   ★ 全置換は「部分的な取得結果で既存データを削る」危険と裏表なので、
     1 系列でも欠けていたら **何も書かずに exit 1** する（validate_series → write の順）。
   冪等性: 同一版の xlsx に対する再実行は CSV が md5 一致する
   （metadata.json の updated_at だけは毎回変わる。これは全 fetcher 共通の仕様）。

--------------------------------------------------------------------------
特殊要件 3: 取得は index ページ経由（xlsx の URL をハードコードしない）
--------------------------------------------------------------------------
2 つの理由が重なっている:
  (a) サイトポリシーに「データファイル(PDF、エクセル、ZIP等)への直接リンクは
      ご遠慮ください」とある。直 URL 決め打ちは規約の精神に反する。
  (b) 実 URL の `-att/` 直前のディレクトリ ID は**公表年ごとにローテートする**
      （2026 年版 k6efli000007q2e2 / 2025 年版 jqjm1000000ie8om / 2024 年版 pi5dm3000010bn3l）。
      ハードコードすると Pink Sheet 型のサイレント停滞（200 応答のまま内部凍結）になる。
→ 毎回 index ページ (docs/source_map.yaml の index_url) をパースして最新版リンクを解決する。

--------------------------------------------------------------------------
特殊要件 4: openpyxl が使えない → zipfile + XML 直パース
--------------------------------------------------------------------------
  - openpyxl の通常ロードは埋め込みドーナツグラフの holeSize=0 で
    `ValueError: Min value is 1` を出して落ちる。read_only=True でも
    iter_rows が空を返す。
  - sharedStrings にルビ（<rPh> 要素）が混入しており、素朴に全 <t> を連結すると
    「発電所・製油所等セイユジョトウ」のようにフリガナが混ざる。
    → <si> 直下の <t> と <r><t> のみを拾い、<rPh> 配下は読まない。

--------------------------------------------------------------------------
採用ブロック（実データで確認。行番号ではなく**ラベル**で引く）
--------------------------------------------------------------------------
レポートは行番号（3.Allocated_CO2-sector の 142-151 など）を挙げているが、
行番号決め打ちは年版で行が 1 つずれた瞬間に静かに壊れる。本実装は
「■ブロック見出し → 年ヘッダ行 → 行ラベル」で引き当て、レポート記載の
想定行番号との一致/不一致をログに出すだけに留める（L-013: 実データで確定する）。

  A 群 3.Allocated_CO2-sector「■排出量 [Mt CO2]」  … 電気・熱配分後の部門別 CO2 9 系列
  B 群 1.Summary「■排出量 [百万トンCO2換算]」      … ガス別 GHG 7 系列
  C 群 1.Summary「■排出・吸収量 [百万トンCO2換算]」… 吸収量・ネット 2 系列（2014 年度〜）

観測日の規約は **年度末**（FY2024 → 2025-03-31）。EDINET 系列と同じ年度末規約に揃える。
（FY 開始日で日付すると最大滞留が約 1108 日になり SLA 800 と 2 倍近くズレる）

出力:
- data/raw/gio/{元ファイル名}.xlsx                （生ファイル）
- data/processed/esg/jp-ghg-*.csv / .parquet      （共通スキーマ long 形式・全置換）
- data/processed/esg/jp-ghg-*.metadata.json       （D-011）
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.http import make_session, session_get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402
from scripts.common.metadata import (  # noqa: E402
    write_metadata_for_expected_indicators,
    write_metadata_for_indicator,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_gio")

SOURCE_KEY = "gio"
REGION = "jp"

# --- OOXML 名前空間 -------------------------------------------------------
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_RNS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# --- ブロック定義 ---------------------------------------------------------
# ラベルは _norm()（空白・全角空白・改行を除去）を通した形で書く。
# 元ファイルのラベルには "エネルギー転換部門\n（電気熱配分統計誤差を除く）" のように
# セル内改行が入っているため、正規化前の文字列で照合してはいけない。

SHEET_SECTOR = "3.Allocated_CO2-sector"
SHEET_SUMMARY = "1.Summary"

BLOCK_SECTOR = "■排出量[MtCO2]"
BLOCK_GAS = "■排出量[百万トンCO2換算]"
BLOCK_NET = "■排出・吸収量[百万トンCO2換算]"

# A 群: 電気・熱配分後の部門別 CO2（9 系列）。
# 「エネルギー転換部門（電気熱配分誤差）」= 統計誤差行は系列化しない（レポート §8 の 18 系列案）。
# ただし合計突合には必要なので SECTOR_DISCREPANCY として別に保持する。
SECTOR_DISCREPANCY = "エネルギー転換部門（電気熱配分誤差）"
SECTOR_TOTAL = "合計"
SECTOR_LABELS: dict[str, str] = {
    "エネルギー転換部門（電気熱配分統計誤差を除く）": "jp-ghg-co2-energy-conversion",
    "産業部門": "jp-ghg-co2-industry",
    "運輸部門": "jp-ghg-co2-transport",
    "業務他部門": "jp-ghg-co2-commercial",
    "家庭部門": "jp-ghg-co2-household",
    "工業プロセス及び製品の使用": "jp-ghg-co2-industrial-process",
    "廃棄物": "jp-ghg-co2-waste",
    "その他（間接CO2等）": "jp-ghg-co2-other",
    SECTOR_TOTAL: "jp-ghg-co2-total",
}
# 合計 = 統計誤差 + 上記 8 部門（＝系列化する 9 行のうち「合計」を除いた 8 行 + 統計誤差行）
SECTOR_COMPONENTS = [SECTOR_DISCREPANCY] + [
    lb for lb in SECTOR_LABELS if lb != SECTOR_TOTAL
]

# 部門は「エネルギー起源」と「非エネルギー起源」にきれいに分かれる。実データで全 35 年度
# 検証したところ、下記 2 本が 1e-12 Mt 精度で成立する（＝ 3.Allocated と 1.Summary の
# 対応が取れている最強の証拠。片方のシートだけ行がずれたら必ず破れる）。
#   5 部門（エネルギー転換/産業/運輸/業務他/家庭）+ 電気熱配分統計誤差 = エネルギー起源 CO2
#   3 部門（工業プロセス/廃棄物/その他）                              = 非エネルギー起源 CO2
SECTOR_ENERGY_ORIGIN = [SECTOR_DISCREPANCY] + [
    "エネルギー転換部門（電気熱配分統計誤差を除く）",
    "産業部門",
    "運輸部門",
    "業務他部門",
    "家庭部門",
]
SECTOR_NONENERGY_ORIGIN = [
    "工業プロセス及び製品の使用",
    "廃棄物",
    "その他（間接CO2等）",
]

# B 群: ガス別 GHG（7 系列）。HFC/PFC/SF6/NF3 の内訳 4 行は「密度版 +4」として
# レポートが挙げているが 18 系列案では採らない。F-gas 合計の突合には使うので保持する。
GAS_TOTAL = "計"
GAS_CO2 = "二酸化炭素（CO2）"
GAS_FGAS = "代替フロン等４ガス"
GAS_LABELS: dict[str, str] = {
    GAS_TOTAL: "jp-ghg-total",
    GAS_CO2: "jp-ghg-co2",
    "エネルギー起源": "jp-ghg-co2-energy-origin",
    "非エネルギー起源": "jp-ghg-co2-nonenergy-origin",
    "メタン（CH4）": "jp-ghg-ch4",
    "一酸化二窒素（N2O）": "jp-ghg-n2o",
    GAS_FGAS: "jp-ghg-fgas",
}
GAS_CO2_PARTS = ["エネルギー起源", "非エネルギー起源"]
GAS_FGAS_PARTS = [
    "ハイドロフルオロカーボン類（HFCs）",
    "パーフルオロカーボン類（PFCs）",
    "六ふっ化硫黄（SF6）",
    "三ふっ化窒素（NF3）",
]
GAS_TOTAL_PARTS = [GAS_CO2, "メタン（CH4）", "一酸化二窒素（N2O）", GAS_FGAS]

# C 群: 吸収量・ネット（2 系列）。実データは 2014 年度〜（1990-2013 は空セル）。
NET_EMISSIONS = "排出量"
NET_REMOVAL = "森林等の吸収源対策による吸収量"
NET_TOTAL = "排出・吸収量（計）"
NET_LABELS: dict[str, str] = {
    NET_REMOVAL: "jp-ghg-lulucf-removal",
    NET_TOTAL: "jp-ghg-net",
}

EXPECTED_SERIES = sorted(
    set(SECTOR_LABELS.values()) | set(GAS_LABELS.values()) | set(NET_LABELS.values())
)

# 突合の許容誤差 [Mt]。元ファイルは倍精度の生値がそのまま入っているので
# 本来は 1e-9 レベルで一致する。0.001 Mt = 1000 t は「丸め由来ではない」と言える幅。
RECONCILE_TOL = 1e-3

# レポート §5 が実測で挙げた想定行番号（照合ログ用。実装はラベル引きなので依存しない）
REPORTED_ROWS = {
    "jp-ghg-co2-energy-conversion": 143,
    "jp-ghg-co2-industry": 144,
    "jp-ghg-co2-transport": 145,
    "jp-ghg-co2-commercial": 146,
    "jp-ghg-co2-household": 147,
    "jp-ghg-co2-industrial-process": 148,
    "jp-ghg-co2-waste": 149,
    "jp-ghg-co2-other": 150,
    "jp-ghg-co2-total": 151,
}


# --- 小道具 ---------------------------------------------------------------


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _norm(text: str | None) -> str:
    """ラベル照合用の正規化: 空白・全角空白・改行・タブをすべて落とす。"""
    if text is None:
        return ""
    return re.sub(r"[\s　]+", "", str(text))


def _col_index(ref: str) -> int:
    """セル参照 "AA142" → 列番号 27（1 始まり）。"""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n


def fiscal_year_end(fiscal_year: int) -> str:
    """年度 → 年度末の日付。FY2024 → "2025-03-31"。"""
    return f"{fiscal_year + 1}-03-31"


# --- 特殊要件 3: index ページから最新 xlsx URL を解決 ----------------------


def resolve_latest_xlsx(session, index_url: str) -> tuple[str, str, str]:
    """
    index ページをパースして最新版 xlsx の絶対 URL を返す。

    Returns:
        (absolute_url, link_text, published_date)  published_date は取れなければ ""

    選び方（3 段構え。どれか 1 つの仕様変更では壊れないようにする）:
      1. 「最新データ」ブロック（class="latestBlock"）内の最初の xls/xlsx リンクを第一候補
      2. ページ全体の xls/xlsx リンクから preliminary（速報値）を除き、
         ファイル名の公表年（L5-7gas_**2026**_gioweb…）が最大のものを第二候補
      3. 1 と 2 が食い違ったら warning を出して 2（公表年最大）を採る

    ★ 速報値（preliminary）は FY2020 分を最後にアーカイブ上に存在しない。
      仮に復活しても確報値と混ぜてはいけないので、常に除外する。
    """
    logger.info("GET %s", index_url)
    resp = session_get(session, index_url)
    resp.raise_for_status()
    # meta charset は UTF-8。requests は Content-Type に charset が無いと
    # ISO-8859-1 と誤推定するので、エンコーディングを推測させずに明示デコードする。
    html = resp.content.decode("utf-8", errors="replace")

    link_re = re.compile(r'<a[^>]+href="([^"]+\.xlsx?)"[^>]*>(.*?)</a>', re.S | re.I)

    def _candidates(segment: str) -> list[tuple[str, str]]:
        out = []
        for m in link_re.finditer(segment):
            href = m.group(1)
            text = _norm(re.sub(r"<[^>]+>", "", m.group(2)))
            if "preliminary" in href.lower():
                continue  # 速報値は確報値と混ぜない
            out.append((href, text))
        return out

    all_links = _candidates(html)
    if not all_links:
        raise RuntimeError(
            f"index ページに xls/xlsx リンクが 1 本も無い（ページ構造変更を疑う）: {index_url}"
        )

    # 1) 最新データブロック
    latest_href = None
    mblock = re.search(r'class="[^"]*latestBlock[^"]*"', html)
    if mblock:
        segment = html[mblock.start(): mblock.start() + 6000]
        block_links = _candidates(segment)
        if block_links:
            latest_href = block_links[0][0]

    # 2) ファイル名の公表年が最大のもの
    year_re = re.compile(r"(?:gas|6gas|7gas)[_-](\d{4})")

    def _pub_year(href: str) -> int:
        m = year_re.search(Path(href).name)
        return int(m.group(1)) if m else -1

    newest_year = max(_pub_year(h) for h, _ in all_links)
    # 同一公表年のリンクが複数ある（改訂版が並ぶ）場合は**文書順で最初**を採る。
    # index ページは新しい順に並んでいるので、最初＝最新の改訂版。
    newest_href, newest_text = next(
        (h, t) for h, t in all_links if _pub_year(h) == newest_year
    )
    if newest_year < 0:
        raise RuntimeError(
            "index ページの xlsx リンクからファイル名の公表年を読めない"
            f"（命名規則の変更を疑う）: {[h for h, _ in all_links[:5]]}"
        )

    # 3) 突合
    if latest_href and latest_href != newest_href:
        logger.warning(
            "最新データブロックのリンク (%s) と公表年最大のリンク (%s) が食い違う。"
            "公表年最大を採用する（index の構造変更を疑うこと）",
            latest_href, newest_href,
        )
    elif latest_href is None:
        logger.warning(
            "index ページに latestBlock が見つからない（構造変更を疑う）。"
            "公表年最大のリンクで代替する"
        )

    # 公表日（<time datetime=YYYY-MM-DD>）が拾えれば記録に残す
    published = ""
    mtime = re.search(
        r"<time[^>]*datetime=\"?([0-9]{4}-[0-9]{2}-[0-9]{2})"
        r"[^>]*>(?:(?!</li>).)*?" + re.escape(newest_href),
        html, re.S,
    )
    if mtime:
        published = mtime.group(1)

    url = urljoin(index_url, newest_href)
    logger.info(
        "解決した最新版: url=%s 公表年=%d 公表日=%s リンク文言=%r（候補 %d 本）",
        url, newest_year, published or "不明", newest_text, len(all_links),
    )
    return url, newest_text, published


# --- 特殊要件 4: zipfile + XML 直パース -----------------------------------


def load_shared_strings(z: zipfile.ZipFile) -> list[str]:
    """
    sharedStrings.xml を読む。**ルビ（<rPh>）は読まない**。

    <si> の直下 <t>、および <r><t> のみを連結する。<rPh> は <si> の子だが
    その配下の <t> を拾うと「製造業セイゾウギョウ」のようにフリガナが混入する。
    （iter() や findall(".//t") で全 <t> をなめる実装にしないこと）
    """
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall(f"{NS}si"):
        parts: list[str] = []
        t = si.find(f"{NS}t")
        if t is not None:
            parts.append(t.text or "")
        for r in si.findall(f"{NS}r"):
            rt = r.find(f"{NS}t")
            if rt is not None:
                parts.append(rt.text or "")
        out.append("".join(parts))
    return out


def sheet_paths(z: zipfile.ZipFile) -> dict[str, str]:
    """シート名 → zip 内パス。"""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid2target = {
        r.get("Id"): r.get("Target")
        for r in rels.findall(f"{PKG_RNS}Relationship")
    }
    out: dict[str, str] = {}
    for sh in wb.find(f"{NS}sheets"):
        target = rid2target[sh.get(f"{RNS}id")]
        out[sh.get("name")] = "xl/" + target.lstrip("/")
    return out


def read_sheet(z: zipfile.ZipFile, path: str, sst: list[str]) -> dict[int, dict[int, str]]:
    """シートを {行番号: {列番号: セル値(str)}} で返す。空セルは持たない。"""
    root = ET.fromstring(z.read(path))
    sheet_data = root.find(f"{NS}sheetData")
    rows: dict[int, dict[int, str]] = {}
    for row in sheet_data.findall(f"{NS}row"):
        cells: dict[int, str] = {}
        for c in row.findall(f"{NS}c"):
            ctype = c.get("t")
            v = c.find(f"{NS}v")
            if ctype == "s" and v is not None:
                value = sst[int(v.text)]
            elif ctype == "inlineStr":
                is_el = c.find(f"{NS}is")
                value = "" if is_el is None else "".join(
                    (t.text or "") for t in is_el.findall(f"{NS}t")
                )
            elif v is not None:
                value = v.text or ""
            else:
                value = ""
            if value is not None and str(value).strip() != "":
                cells[_col_index(c.get("r"))] = value
        rows[int(row.get("r"))] = cells
    return rows


def _row_label(cells: dict[int, str], first_year_col: int) -> str:
    """
    行ラベル = 年ブロックより左にある最小列のセル。

    Summary シートは階層で列がずれる（大分類は列 19、内訳は列 20）ので
    列番号を決め打ちにせず「年ブロックより左の最小列」を採る。
    """
    left = [c for c in cells if c < first_year_col]
    return _norm(cells[min(left)]) if left else ""


def extract_block(
    rows: dict[int, dict[int, str]],
    block_marker: str,
    *,
    sheet_name: str,
) -> tuple[dict[str, dict[int, float]], dict[str, int]]:
    """
    「■…」ブロック見出しを探し、その直後の年ヘッダ行と各データ行を読む。

    Returns:
        (values, row_of_label)
        values      : {正規化ラベル: {年度: 値}}
        row_of_label: {正規化ラベル: 元ファイルの行番号}（照合ログ用）
    """
    start = None
    for r in sorted(rows):
        cells = rows[r]
        if any(_norm(v) == block_marker for v in cells.values()):
            start = r
            break
    if start is None:
        raise RuntimeError(
            f"{sheet_name}: ブロック見出し {block_marker!r} が見つからない"
            "（シート構成の変更を疑う）"
        )

    # 見出しの下から年ヘッダ行（4 桁年が 2 つ以上並ぶ行）を探す
    header_row = None
    year_cols: dict[int, int] = {}
    for r in range(start, start + 6):
        cells = rows.get(r, {})
        found = {
            c: int(v) for c, v in cells.items()
            if re.fullmatch(r"(19|20)\d{2}", str(v).strip())
        }
        if len(found) >= 2:
            header_row, year_cols = r, found
            break
    if header_row is None:
        raise RuntimeError(
            f"{sheet_name}/{block_marker}: 年ヘッダ行が見つからない（見出し行 {start}）"
        )

    first_year_col = min(year_cols)
    values: dict[str, dict[int, float]] = {}
    row_of_label: dict[str, int] = {}

    for r in range(header_row + 1, header_row + 40):
        cells = rows.get(r, {})
        if not cells:
            break  # 空行でブロック終了
        label = _row_label(cells, first_year_col)
        if not label or label.startswith("■"):
            break
        series: dict[int, float] = {}
        for col, year in year_cols.items():
            raw = cells.get(col)
            if raw is None or str(raw).strip() in {"", "-", "－", "ー", "NA", "n.a."}:
                continue
            try:
                series[year] = float(raw)
            except ValueError:
                logger.warning(
                    "%s/%s 行%d %s の %d 年度が数値化できない: %r（この 1 点のみ落とす）",
                    sheet_name, block_marker, r, label, year, raw,
                )
        values[label] = series
        row_of_label[label] = r

    logger.info(
        "%s/%s: 見出し行=%d 年ヘッダ行=%d 年度=%d..%d データ行=%d",
        sheet_name, block_marker, start, header_row,
        min(year_cols.values()), max(year_cols.values()), len(values),
    )
    return values, row_of_label


# --- 突合（書き出し前の整合性検査）----------------------------------------


def _reconcile(
    name: str,
    total: dict[int, float],
    parts: Iterable[dict[int, float]],
    errors: list[str],
) -> None:
    """total == sum(parts) を年度ごとに検査し、ずれを errors に積む。"""
    parts = list(parts)
    checked = 0
    worst = (0.0, None)
    for year, tv in sorted(total.items()):
        got = [p.get(year) for p in parts]
        if any(g is None for g in got):
            continue  # 部品が揃わない年度（C 群の 2013 年度以前など）はスキップ
        diff = abs(sum(got) - tv)
        checked += 1
        if diff > worst[0]:
            worst = (diff, year)
        if diff > RECONCILE_TOL:
            errors.append(
                f"{name}: {year} 年度 合計 {tv:.6f} ≠ 内訳合計 {sum(got):.6f} "
                f"(差 {diff:.6f} Mt > 許容 {RECONCILE_TOL})"
            )
    logger.info(
        "突合 %s: %d 年度を検査、最大差 %.3e Mt (%s 年度)",
        name, checked, worst[0], worst[1],
    )


def check_integrity(
    sector: dict[str, dict[int, float]],
    gas: dict[str, dict[int, float]],
    net: dict[str, dict[int, float]],
) -> list[str]:
    """
    元ファイルが内部で持っている恒等式を突合する。
    1 本でも破れていたら「シート/行/列の取り違え」を疑うべきなので書き出しを中止する。
    """
    errors: list[str] = []

    # 1) 部門別 CO2: 合計 = 統計誤差 + 8 部門
    _reconcile(
        "Allocated CO2 部門合計",
        sector[SECTOR_TOTAL],
        (sector[lb] for lb in SECTOR_COMPONENTS),
        errors,
    )
    # 2) ガス別: CO2 = エネルギー起源 + 非エネルギー起源
    _reconcile("CO2 起源別内訳", gas[GAS_CO2], (gas[lb] for lb in GAS_CO2_PARTS), errors)
    # 3) ガス別: 代替フロン等4ガス = HFCs + PFCs + SF6 + NF3
    _reconcile("代替フロン等4ガス内訳", gas[GAS_FGAS], (gas[lb] for lb in GAS_FGAS_PARTS), errors)
    # 4) ガス別: 計 = CO2 + CH4 + N2O + 代替フロン等4ガス
    _reconcile("GHG 総排出量", gas[GAS_TOTAL], (gas[lb] for lb in GAS_TOTAL_PARTS), errors)
    # 5) ネット: 排出・吸収量（計） = 排出量 + 吸収量（吸収量は負値）
    _reconcile("排出・吸収量", net[NET_TOTAL], (net[NET_EMISSIONS], net[NET_REMOVAL]), errors)

    # 6) シート跨ぎ: 3.Allocated の部門合計 == 1.Summary の CO2 排出量
    #    jp-ghg-co2-total（A 群）と jp-ghg-co2（B 群）は同一の値になる系列で、
    #    レポート §8 の 18 系列案は両方を採る。冗長ではあるが、別シート由来の
    #    2 本が一致することは「どちらかのシートを取り違えていない」証拠になるので
    #    ここで恒等式として使う。
    _reconcile(
        "3.Allocated 部門合計 vs 1.Summary CO2（シート跨ぎ）",
        sector[SECTOR_TOTAL],
        (gas[GAS_CO2],),
        errors,
    )
    # 7) ネットの排出量 == ガス別の計（同一シート内の別ブロック）
    _reconcile("1.Summary 排出量 vs ガス別計", net[NET_EMISSIONS], (gas[GAS_TOTAL],), errors)

    # 8-9) シート跨ぎ・起源別: 部門を エネルギー起源 / 非エネルギー起源 に割り付けた突合。
    #      合計同士の比較（6）より強い。合計が合っていても部門の並びが 1 行ずれていれば
    #      こちらが破れるため、行の対応が取れていることの証拠になる。
    _reconcile(
        "エネルギー起源 CO2 vs 5 部門+統計誤差（シート跨ぎ）",
        gas["エネルギー起源"],
        (sector[lb] for lb in SECTOR_ENERGY_ORIGIN),
        errors,
    )
    _reconcile(
        "非エネルギー起源 CO2 vs 非エネ 3 部門（シート跨ぎ）",
        gas["非エネルギー起源"],
        (sector[lb] for lb in SECTOR_NONENERGY_ORIGIN),
        errors,
    )

    return errors


def check_sanity_bands(df: pd.DataFrame, source_cfg: dict) -> list[str]:
    """
    最新観測が想定水準にあるかを **書き出し前に** 検査する（fetch_nonfossil.py と同じ作法）。

    狙いは「単位（Mt / 千トン / トン）やシートを取り違えて桁違いの値を着地させる」ことの
    予防。枠は source_map.yaml の indicators.{id}.sanity_min / sanity_max に置く。
    """
    errors: list[str] = []
    indicators = source_cfg.get("indicators") or {}
    for indicator_id, group in df.groupby("indicator_id"):
        cfg = indicators.get(str(indicator_id)) or {}
        lo, hi = cfg.get("sanity_min"), cfg.get("sanity_max")
        if lo is None or hi is None:
            continue
        latest = group.sort_values("date").iloc[-1]
        if not (lo <= latest["value"] <= hi):
            errors.append(
                f"{indicator_id}: 最新観測 {latest['date']} = {latest['value']:.3f} が "
                f"想定枠 [{lo}, {hi}] の外。単位・シート・行の取り違えを疑うこと"
            )
        else:
            logger.info(
                "サニティ %s: 最新 %s = %.3f（枠 [%s, %s] 内）",
                indicator_id, latest["date"], latest["value"], lo, hi,
            )
    return errors


# --- 組み立て -------------------------------------------------------------


def build_frame(
    blocks: list[tuple[dict[str, dict[int, float]], dict[str, str]]],
    source_url: str,
) -> pd.DataFrame:
    """(ブロック値, ラベル→indicator_id) の並びから共通スキーマ long 形式を作る。"""
    rows: list[dict] = []
    for values, label_map in blocks:
        for label, indicator_id in label_map.items():
            for year, value in sorted(values[label].items()):
                rows.append({
                    "date": fiscal_year_end(year),
                    "indicator_id": indicator_id,
                    "region": REGION,
                    "value": value,
                    "source_url": source_url,
                })
    return pd.DataFrame(rows, columns=["date", "indicator_id", "region", "value", "source_url"])


def validate_series(df: pd.DataFrame) -> list[str]:
    """
    全 18 系列が揃っているかを検査する。

    ★ 全置換（replace=True）は「取れなかった系列の既存データを削る」危険と裏表なので、
      1 系列でも欠けていたら何も書かずに落とす。
    """
    errors: list[str] = []
    got = set(df["indicator_id"].unique())
    missing = [s for s in EXPECTED_SERIES if s not in got]
    unexpected = sorted(got - set(EXPECTED_SERIES))
    if missing:
        errors.append(f"系列が欠けている（全置換は中止）: {missing}")
    if unexpected:
        errors.append(f"想定外の系列 id が生成された: {unexpected}")
    for indicator_id, group in df.groupby("indicator_id"):
        if len(group) == 0:
            errors.append(f"{indicator_id}: 観測 0 件")
        if group["value"].isna().any():
            errors.append(f"{indicator_id}: NaN を含む")
    return errors


def log_row_crosscheck(row_of_label: dict[str, int]) -> None:
    """レポート §5 が実測で挙げた行番号との一致をログに出す（実装は依存しない）。"""
    for label, indicator_id in SECTOR_LABELS.items():
        expected = REPORTED_ROWS.get(indicator_id)
        actual = row_of_label.get(label)
        if expected is None or actual is None:
            continue
        if expected != actual:
            logger.info(
                "行番号の移動: %s はレポート想定 %d 行目 → 実測 %d 行目"
                "（ラベル引きなので影響なし）",
                indicator_id, expected, actual,
            )


# --- main -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Japan's GHG inventory (NIES/GIO) — 18 series, esg domain",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="取得・パース・突合まで行い、data/processed には書き出さない",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    index_url: str = source_cfg["index_url"]
    raw_dir = ROOT / "data" / "raw" / "gio"
    processed_dir = ROOT / "data" / "processed" / "esg"
    log_dir = ROOT / "data" / "_logs"

    session = make_session()

    # 1) 特殊要件 3: index ページから最新版 xlsx URL を解決（URL はハードコードしない）
    try:
        xlsx_url, link_text, published = resolve_latest_xlsx(session, index_url)
    except Exception as e:
        msg = f"最新版 URL の解決に失敗: {e}"
        logger.exception(msg)
        append_log(log_dir, "fetch_gio", "FAIL", msg)
        return 1

    # 2) ダウンロード
    logger.info("GET %s", xlsx_url)
    resp = session_get(session, xlsx_url, headers={"Referer": index_url}, timeout=120)
    if resp.status_code >= 400:
        msg = f"xlsx HTTP {resp.status_code}: {xlsx_url}"
        logger.error(msg)
        append_log(log_dir, "fetch_gio", "FAIL", msg)
        return 1
    content = resp.content
    if content[:2] != b"PK":
        msg = (
            f"xlsx ではない応答（先頭 {content[:16]!r}, {len(content)} bytes）: {xlsx_url}"
            " — リンク切れで HTML が返っている可能性"
        )
        logger.error(msg)
        append_log(log_dir, "fetch_gio", "FAIL", msg)
        return 1

    filename = Path(xlsx_url).name
    md5 = hashlib.md5(content).hexdigest()
    logger.info("取得: %s (%d bytes, md5=%s)", filename, len(content), md5)
    save_raw(content, raw_dir, filename)

    # 3) 特殊要件 4: zipfile + XML 直パース（openpyxl は使えない）
    try:
        z = zipfile.ZipFile(BytesIO(content))
        sst = load_shared_strings(z)
        paths = sheet_paths(z)
        missing_sheets = [s for s in (SHEET_SECTOR, SHEET_SUMMARY) if s not in paths]
        if missing_sheets:
            raise RuntimeError(
                f"必要なシートが無い: {missing_sheets} / 実在シート={list(paths)}"
            )
        sector_rows = read_sheet(z, paths[SHEET_SECTOR], sst)
        summary_rows = read_sheet(z, paths[SHEET_SUMMARY], sst)

        sector, sector_row_of = extract_block(
            sector_rows, BLOCK_SECTOR, sheet_name=SHEET_SECTOR)
        gas, _ = extract_block(summary_rows, BLOCK_GAS, sheet_name=SHEET_SUMMARY)
        net, _ = extract_block(summary_rows, BLOCK_NET, sheet_name=SHEET_SUMMARY)
    except Exception as e:
        msg = f"xlsx のパースに失敗: {e}"
        logger.exception(msg)
        append_log(log_dir, "fetch_gio", "FAIL", msg)
        return 1

    log_row_crosscheck(sector_row_of)

    # 4) ラベルが全部そろっているか（年版で表記が変わったらここで気付く）
    label_errors: list[str] = []
    for label in list(SECTOR_LABELS) + SECTOR_COMPONENTS:
        if label not in sector:
            label_errors.append(f"{SHEET_SECTOR}: 行ラベル {label!r} が見つからない")
    for label in list(GAS_LABELS) + GAS_CO2_PARTS + GAS_FGAS_PARTS:
        if label not in gas:
            label_errors.append(f"{SHEET_SUMMARY}/{BLOCK_GAS}: 行ラベル {label!r} が見つからない")
    for label in list(NET_LABELS) + [NET_EMISSIONS]:
        if label not in net:
            label_errors.append(f"{SHEET_SUMMARY}/{BLOCK_NET}: 行ラベル {label!r} が見つからない")
    if label_errors:
        for e in label_errors:
            logger.error("%s", e)
        logger.error(
            "実在ラベル: sector=%s / gas=%s / net=%s",
            list(sector), list(gas), list(net),
        )
        msg = "行ラベル不一致（何も書き出していない）: " + " / ".join(label_errors)
        append_log(log_dir, "fetch_gio", "FAIL", msg)
        return 1

    # 5) 元ファイル内部の恒等式を突合（取り違え検出）
    integrity_errors = check_integrity(sector, gas, net)
    if integrity_errors:
        for e in integrity_errors:
            logger.error("整合性エラー: %s", e)
        msg = "整合性エラー（何も書き出していない）: " + " / ".join(integrity_errors)
        append_log(log_dir, "fetch_gio", "FAIL", msg)
        return 1

    # 6) 共通スキーマへ
    df = build_frame(
        [(sector, SECTOR_LABELS), (gas, GAS_LABELS), (net, NET_LABELS)],
        source_url=xlsx_url,
    )

    series_errors = validate_series(df)
    band_errors = check_sanity_bands(df, source_cfg)
    if series_errors or band_errors:
        for e in series_errors + band_errors:
            logger.error("検証エラー: %s", e)
        msg = "検証エラー（何も書き出していない）: " + " / ".join(series_errors + band_errors)
        append_log(log_dir, "fetch_gio", "FAIL", msg)
        return 1

    latest_year = max(int(d[:4]) - 1 for d in df["date"])
    total_latest = df[
        (df["indicator_id"] == "jp-ghg-total")
        & (df["date"] == fiscal_year_end(latest_year))
    ]["value"]
    logger.info(
        "最新年度 = FY%d（観測日 %s）/ 総排出量 = %.3f Mt-CO2e",
        latest_year, fiscal_year_end(latest_year),
        float(total_latest.iloc[0]) if len(total_latest) else float("nan"),
    )

    if args.dry_run:
        logger.info(
            "--dry-run: 検証まで完了（%d 系列 / %d 行 / %s..%s）。書き出しはしない",
            df["indicator_id"].nunique(), len(df), df["date"].min(), df["date"].max(),
        )
        return 0

    # 7) 特殊要件 2: 追記ではなく全置換で書き出す
    for indicator_id, group in df.groupby("indicator_id"):
        write_processed(group, processed_dir, basename=str(indicator_id), replace=True)
        write_metadata_for_indicator(processed_dir, source_cfg, str(indicator_id), group)

    # D-020④: フェッチ成功範囲で行が来なかった indicator も metadata を書き直す
    # （updated_at = 生存信号）。GIO は 1 xlsx を全置換で書き出し、
    # ラベル照合・整合性・バンド検証を全通過した後にしか到達しない（= 全系列成功）。
    # --dry-run はこの手前で return するのでここは通らない。
    expected_ids = set(source_cfg.get("indicator_ids") or [])
    meta_refreshed, meta_skipped = write_metadata_for_expected_indicators(
        processed_dir, source_cfg, sorted(expected_ids - set(df["indicator_id"].astype(str)))
    )
    logger.info(
        "metadata refreshed for row-less indicators: %d (skipped=%d)",
        len(meta_refreshed), len(meta_skipped),
    )
    if meta_skipped:
        logger.warning(
            "metadata refresh skipped (no CSV / unreadable cutoff): %s",
            ", ".join(meta_skipped),
        )

    per_series = ", ".join(
        f"{iid}={len(g)}" for iid, g in sorted(df.groupby("indicator_id"))
    )
    summary = (
        f"file={filename} md5={md5} published={published or 'unknown'} "
        f"series={df['indicator_id'].nunique()} rows={len(df)} "
        f"range={df['date'].min()}..{df['date'].max()} latest_fy={latest_year} "
        f"mode=full-replace metadata_refreshed={len(meta_refreshed)} [{per_series}]"
    )
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_gio", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
