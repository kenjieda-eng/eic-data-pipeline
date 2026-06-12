#!/usr/bin/env python3
"""資源エネルギー庁「なっとく！再生可能エネルギー」FIT 調達価格 電源別×年度 seed。

regulation ドメイン（北極星 12 ドメイン seed の最後の 1 つ）。
日本の再エネ FIT 制度の根幹データ = 電源別の年度別 調達価格（円/kWh）。

------------------------------------------------------------------------------
取得源（curl_cffi/chrome120 で Akamai Bot Manager を回避。fetch_enecho_power.py 流用）:
  - 過去の買取価格（2012-2025 年度）:
      https://www.enecho.meti.go.jp/category/saving_and_new/saiene/kaitori/kakaku.html
  - 現行（2026 年度以降）:
      https://www.enecho.meti.go.jp/category/saving_and_new/saiene/kaitori/fit_kakaku.html
  両ページとも UTF-8 の HTML 表（PDF 依存なし）。h3「YYYY年度の価格表」(平成/令和は西暦換算)
  で年度ブロックを区切り、その下に電源別 <table> が並ぶ。列構成は年度で激変するため、
  <table> を colspan/rowspan 展開した grid 上で「列見出しキーワード + 除外 + pick」により
  各電源の代表区分の値を抽出する（L-062: 価格はハードコードせず live HTML から取得）。

第 1 弾スコープ（5 系列、円/kWh、年度別 date=YYYY-04-01、domain=regulation）:
  fit-price-solar-business : 事業用太陽光（10kW以上, 非入札・非屋根の代表区分）
  fit-price-wind-onshore   : 陸上風力（非入札・非リプレースの代表区分）
  fit-price-geothermal     : 地熱（15,000kW未満・新設）
  fit-price-biomass-wood   : 一般木質バイオマス（入札対象外の代表区分）
  fit-price-hydro-small    : 中小水力（200kW未満・新設）
  取れない年/電源（2026 の連続価格・入札・※ プレースホルダ等）は無理せず skip して報告。

ライセンス（L-063 = GO）:
  資源エネルギー庁（METI）公表。政府標準利用規約 2.0（CC BY 互換）。出典明記で再利用可。
  license id は既存 meti-terms を流用（enecho-power と同じ）。

出力（D-017: processed_dir=regulation）:
  - data/raw/fit/{past,current}_{YYYYMMDD}.html
  - data/processed/regulation/{indicator_id}.csv / .parquet / .metadata.json

CLI:
  python scripts/fetch_fit_price.py --explore   # 抽出結果を年度×系列で一覧表示（CSV は書かない）
  python scripts/fetch_fit_price.py             # 取得 → CSV/metadata 書き出し
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

try:
    from curl_cffi import requests as _curl_requests  # noqa: E402
    _CURL_CFFI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _curl_requests = None
    _CURL_CFFI_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_fit_price")

SOURCE_KEY = "fit"
DATA_ROOT = REPO_ROOT / "data"
PROCESSED_DIR = DATA_ROOT / "processed" / "regulation"
RAW_DIR = DATA_ROOT / "raw" / "fit"
LOG_DIR = DATA_ROOT / "_logs"

PAST_URL = "https://www.enecho.meti.go.jp/category/saving_and_new/saiene/kaitori/kakaku.html"
CURRENT_URL = "https://www.enecho.meti.go.jp/category/saving_and_new/saiene/kaitori/fit_kakaku.html"

# fetch_enecho_power.py と同じヘッダ方針（curl_cffi が UA を impersonate）
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}


def _meti_get(url: str, *, timeout: int = 120):
    if not _CURL_CFFI_AVAILABLE:
        raise RuntimeError(
            "curl_cffi is required to fetch METI (Akamai Bot Manager bypass). "
            "Install via: pip install curl_cffi"
        )
    r = _curl_requests.get(url, impersonate="chrome120", timeout=timeout, headers=_HEADERS)
    return r


# ---------------------------------------------------------------------------
# HTML テーブルの colspan/rowspan 展開
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    """空白を除去して連結（METI 表は改行・全角空白で区切られるため）。"""
    return _WS.sub("", (s or "")).replace("　", "")


def expand_grid(table) -> list[list[str]]:
    """<table> を colspan/rowspan を考慮した 2 次元 grid（テキスト）に展開する。"""
    grid: dict[tuple[int, int], str] = {}
    rowspan_left: dict[int, tuple[int, str]] = {}  # col -> (remaining_rows, text)
    rows = table.find_all("tr")
    max_col = 0
    for r, tr in enumerate(rows):
        c = 0
        cells = tr.find_all(["th", "td"])
        ci = 0
        # まず rowspan 継続セルを置く
        while True:
            # rowspan 継続でこの列が埋まっているか確認しつつ、空き列に td を流し込む
            if c in rowspan_left and rowspan_left[c][0] > 0:
                rem, txt = rowspan_left[c]
                grid[(r, c)] = txt
                rowspan_left[c] = (rem - 1, txt)
                if rowspan_left[c][0] == 0:
                    del rowspan_left[c]
                c += 1
                continue
            if ci >= len(cells):
                break
            cell = cells[ci]
            ci += 1
            txt = cell.get_text(" ", strip=True)
            try:
                cs = int(cell.get("colspan", 1))
            except (TypeError, ValueError):
                cs = 1
            try:
                rs = int(cell.get("rowspan", 1))
            except (TypeError, ValueError):
                rs = 1
            for k in range(cs):
                grid[(r, c)] = txt
                if rs > 1:
                    rowspan_left[c] = (rs - 1, txt)
                c += 1
        max_col = max(max_col, c)
    n_rows = len(rows)
    out: list[list[str]] = []
    for r in range(n_rows):
        out.append([grid.get((r, c), "") for c in range(max_col)])
    return out


# 値セルの「N円」を抽出（複数あれば最後＝経過措置後の標準額を採用）
_YEN = re.compile(r"(\d+(?:\.\d+)?)\s*円")


def parse_price(cell: str) -> Optional[float]:
    """セル文字列から円/kWh の数値を返す。複数の「N円」があれば最後を採用。"""
    if not cell:
        return None
    nums = _YEN.findall(cell)
    if not nums:
        return None
    try:
        return float(nums[-1])
    except ValueError:
        return None


def _is_value_row(cells: list[str]) -> bool:
    joined = "".join(cells)
    if "調達期間" in joined or "交付期間" in joined:
        return False
    return ("円" in joined) or ("入札制度により決定" in joined)


def _column_headers(grid: list[list[str]], value_row_idx: int) -> dict[int, str]:
    """value_row より上の全行を列ごとに連結した正規化見出しを返す。"""
    n_cols = len(grid[0]) if grid else 0
    headers: dict[int, str] = {c: "" for c in range(n_cols)}
    for r in range(value_row_idx):
        for c in range(n_cols):
            if c < len(grid[r]):
                headers[c] += grid[r][c]
    return {c: _norm(h) for c, h in headers.items()}


# ---------------------------------------------------------------------------
# 系列ごとの抽出仕様
# ---------------------------------------------------------------------------

# col_keywords: 優先順（最初にマッチした 1 つを採用）。すべて _norm 済み前提。
# excludes: 列見出しにこのいずれかを含む列は除外。
# pick: 複数マッチ時 "first"（table→col 昇順）/ "last"（最後の col）。
SERIES_SPECS = [
    {
        "id": "fit-price-solar-business",
        "name": "FIT 調達価格 事業用太陽光（10kW以上・非入札）",
        "col_keywords": ["10kW以上50kW未満", "10kW以上500kW未満",
                          "10kW以上2,000kW未満", "10kW以上2000kW未満", "10kW以上"],
        "excludes": ["屋根", "ダブル", "10kW未満"],
        "pick": "last",
        "notes": ("事業用太陽光の非入札・非屋根の代表区分（年により 10kW以上 / 10kW以上50kW未満 等）。"
                  "上期(4-9月)額を採用。10kW以上の調達価格は原則税抜（消費税は別途交付）。"),
    },
    {
        "id": "fit-price-wind-onshore",
        "name": "FIT 調達価格 陸上風力（非入札・非リプレース）",
        "col_keywords": ["陸上風力", "20kW以上"],
        "excludes": ["洋上", "リプレース", "入札制度適用区分", "50kW以上", "250kW", "ダブル", "20kW未満"],
        "pick": "first",
        "notes": ("陸上風力の非入札・非リプレースの代表区分（年により 20kW以上 / 陸上風力 / 陸上風力50kW未満）。"
                  "2017年度以前は税抜（円+税）。大規模は近年入札移行のため非入札区分を採用。"),
    },
    {
        "id": "fit-price-geothermal",
        "name": "FIT 調達価格 地熱（15,000kW未満・新設）",
        "col_keywords": ["15,000kW未満"],
        "excludes": ["リプレース", "全設備更新", "地下設備流用"],
        "pick": "first",
        "notes": "地熱 15,000kW未満・新設（リプレース除く）。原則税抜。",
    },
    {
        "id": "fit-price-biomass-wood",
        "name": "FIT 調達価格 一般木質バイオマス（入札対象外）",
        "col_keywords": ["一般木質", "一般木材"],
        "excludes": ["入札制度適用区分"],
        "pick": "last",
        "notes": ("一般木質バイオマス（固体燃料）の入札対象外の代表区分。"
                  "近年は 10,000kW未満（10,000kW以上は入札）。原則税抜。"),
    },
    {
        "id": "fit-price-hydro-small",
        "name": "FIT 調達価格 中小水力（200kW未満・新設）",
        "col_keywords": ["200kW未満"],
        "excludes": ["既設導水路"],
        "pick": "first",
        "notes": "中小水力 200kW未満・新設（既設導水路活用＝リプレース除く）。原則税抜。",
    },
]

YEAR_H3 = re.compile(r"(平成|令和|)\s*(\d{1,4})\s*年度(?:以降)?の価格表")


def _to_seireki(era: str, n: str) -> int:
    n = int(n)
    if era == "平成":
        return 1988 + n
    if era == "令和":
        return 2018 + n
    return n


def group_tables_by_year(html: str) -> dict[int, list[list[list[str]]]]:
    """ページ HTML を {fiscal_year: [expanded_grid, ...]} にグルーピング。"""
    soup = BeautifulSoup(html, "html.parser")
    by_year: dict[int, list[list[list[str]]]] = {}
    cur_year: Optional[int] = None
    for el in soup.find_all(["h3", "table"]):
        if el.name == "h3":
            m = YEAR_H3.search(el.get_text(" ", strip=True))
            if m:
                cur_year = _to_seireki(m.group(1), m.group(2))
                by_year.setdefault(cur_year, [])
        elif el.name == "table" and cur_year is not None:
            by_year[cur_year].append(expand_grid(el))
    return by_year


def _value_row_index(grid: list[list[str]], year_label: Optional[str]) -> Optional[int]:
    """price 行の index を返す。

    year_label が指定された場合（current ページ）: 先頭セルにその年度を含み「参考」を含まない値行。
    None の場合（past ページ）: 最初の値行（太陽光の 4-9月 / 10-3月では 4-9月）。
    """
    value_rows = [r for r in range(len(grid)) if _is_value_row(grid[r])]
    if not value_rows:
        return None
    if year_label is None:
        return value_rows[0]
    for r in value_rows:
        label = _norm(grid[r][0]) if grid[r] else ""
        if year_label in label and "参考" not in label:
            return r
    return None


def extract_series_value(
    year_tables: list[list[list[str]]],
    spec: dict,
    *,
    year_label: Optional[str] = None,
) -> Optional[tuple[float, str, str]]:
    """1 年度分のテーブル群から spec の系列値を抽出。

    Returns: (value, raw_cell, header_text) or None（取得不可）。
    """
    excludes = spec["excludes"]
    for kw in spec["col_keywords"]:
        matches: list[tuple[int, int, float, str, str]] = []  # (t_idx, col, val, raw, header)
        for t_idx, grid in enumerate(year_tables):
            if not grid:
                continue
            vr = _value_row_index(grid, year_label)
            if vr is None:
                continue
            headers = _column_headers(grid, vr)
            for c, htext in headers.items():
                if kw not in htext:
                    continue
                if any(x in htext for x in excludes):
                    continue
                if c >= len(grid[vr]):
                    continue
                val = parse_price(grid[vr][c])
                if val is None:
                    continue
                matches.append((t_idx, c, val, grid[vr][c], htext))
        if not matches:
            continue
        if spec["pick"] == "first":
            matches.sort(key=lambda m: (m[0], m[1]))
            chosen = matches[0]
        else:  # last: 同一 table の最後の col を優先
            matches.sort(key=lambda m: (m[0], m[1]))
            chosen = matches[-1]
        return chosen[2], chosen[3], chosen[4]
    return None


# ---------------------------------------------------------------------------
# パイプライン
# ---------------------------------------------------------------------------

def collect_all(today_tag: str, *, save_raw_files: bool = True) -> dict[str, dict[int, tuple[float, str, str]]]:
    """両ページを取得し {series_id: {year: (value, raw, header)}} を返す。"""
    results: dict[str, dict[int, tuple[float, str, str]]] = {s["id"]: {} for s in SERIES_SPECS}

    # past ページ: 2012-2025
    r = _meti_get(PAST_URL)
    r.raise_for_status()
    past_html = r.content.decode("utf-8", errors="replace")
    if save_raw_files:
        save_raw(past_html.encode("utf-8"), RAW_DIR, f"past_{today_tag}.html")
    past_by_year = group_tables_by_year(past_html)
    logger.info("past page: years=%s", sorted(past_by_year))
    for year, tables in past_by_year.items():
        for spec in SERIES_SPECS:
            got = extract_series_value(tables, spec)
            if got is not None:
                results[spec["id"]][year] = got

    # current ページ: 2026（値行ラベルに「2026年度」を含む行のみ）
    r = _meti_get(CURRENT_URL)
    r.raise_for_status()
    cur_html = r.content.decode("utf-8", errors="replace")
    if save_raw_files:
        save_raw(cur_html.encode("utf-8"), RAW_DIR, f"current_{today_tag}.html")
    cur_by_year = group_tables_by_year(cur_html)
    logger.info("current page: years=%s", sorted(cur_by_year))
    for year, tables in cur_by_year.items():
        label = f"{year}年度"
        for spec in SERIES_SPECS:
            got = extract_series_value(tables, spec, year_label=label)
            if got is not None:
                results[spec["id"]][year] = got

    return results


def _source_url_for_year(year: int) -> str:
    return CURRENT_URL if year >= 2026 else PAST_URL


def build_and_write(results: dict[str, dict[int, tuple[float, str, str]]]) -> tuple[int, int]:
    """series×year を long CSV + metadata に書き出す。Returns (n_series, n_rows)。"""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    n_series = 0
    n_rows = 0
    for spec in SERIES_SPECS:
        ind_id = spec["id"]
        year_map = results.get(ind_id, {})
        if not year_map:
            logger.warning("%s: 0 years extracted — skip", ind_id)
            continue
        rows = []
        for year in sorted(year_map):
            val, _raw, _hdr = year_map[year]
            rows.append({
                "date": f"{year:04d}-04-01",
                "indicator_id": ind_id,
                "region": "jp",
                "value": val,
                "source_url": _source_url_for_year(year),
            })
        df = pd.DataFrame(rows, columns=["date", "indicator_id", "region", "value", "source_url"])
        source_cfg = {
            "name": "資源エネルギー庁「なっとく！再生可能エネルギー」FIT 買取価格",
            "publisher": "資源エネルギー庁（METI）",
            "publisher_url": "https://www.enecho.meti.go.jp/",
            "source_url": _source_url_for_year(max(year_map)),
            "license": "meti-terms",
            "license_url": "https://www.meti.go.jp/main/rules.html",
            "license_notice": "出典: 資源エネルギー庁「なっとく！再生可能エネルギー」FIT・FIP制度 買取価格。政府標準利用規約 2.0 に従い、出典明示を条件に利用可。",
            "frequency": "annual",
            "tz": "Asia/Tokyo",
            "missing_policy": "null",
            "backfill_start": "2012-04-01",
            "freshness_sla_days": 540,
            "indicators": {
                ind_id: {
                    "name": spec["name"],
                    "domain": "regulation",
                    "unit": "円/kWh",
                    "aggregation": "raw",
                    "backfill_start": "2012-04-01",
                    "notes": spec["notes"],
                    "depends_on": None,
                }
            },
        }
        write_processed(df, PROCESSED_DIR, basename=ind_id)
        write_metadata_for_indicator(PROCESSED_DIR, source_cfg, ind_id, df)
        n_series += 1
        n_rows += len(df)
        logger.info("%s: %d years (%s..%s), latest=%s 円/kWh",
                    ind_id, len(df), df["date"].min(), df["date"].max(), rows[-1]["value"])
    return n_series, n_rows


def explore(results: dict[str, dict[int, tuple[float, str, str]]]) -> None:
    """抽出結果を年度×系列で一覧表示（検証用、CSV は書かない）。"""
    out = []
    for spec in SERIES_SPECS:
        ind_id = spec["id"]
        ym = results.get(ind_id, {})
        out.append("=" * 80)
        out.append(f"{ind_id}  ({spec['name']})")
        for year in sorted(ym):
            val, raw, hdr = ym[year]
            out.append(f"  {year}: {val:>6} 円/kWh   raw='{raw}'   col='{hdr[:60]}'")
        missing = [y for y in range(2012, 2027) if y not in ym]
        out.append(f"  -> {len(ym)} years; missing: {missing}")
    text = "\n".join(out)
    (REPO_ROOT / "_explore_fit_extracted.txt").write_text(text, encoding="utf-8")
    logger.info("wrote _explore_fit_extracted.txt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch METI FIT 調達価格 電源別 (regulation seed)")
    parser.add_argument("--explore", action="store_true",
                        help="抽出結果を年度×系列で一覧表示（CSV は書かない）")
    args = parser.parse_args(argv)

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    try:
        results = collect_all(today_tag, save_raw_files=not args.explore)
    except Exception as e:
        logger.exception("collect failed: %s", e)
        append_log(LOG_DIR, "fetch_fit_price", "error", str(e))
        return 1

    if args.explore:
        explore(results)
        return 0

    n_series, n_rows = build_and_write(results)
    if n_series == 0:
        logger.error("no series produced rows")
        append_log(LOG_DIR, "fetch_fit_price", "FAIL", "no series produced rows")
        return 1
    logger.info("done: %d series, %d rows -> %s", n_series, n_rows, PROCESSED_DIR)
    append_log(LOG_DIR, "fetch_fit_price", "ok", f"series={n_series} rows={n_rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
