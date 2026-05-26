"""
中国 製造業 PMI（NBS 官製）取得スクリプト（Phase 2 international, 1 系列）。

対象（source_map.yaml の `nbs-pmi` セクション）:
    - china-nbs-mfg-pmi : 国家統計局 月次 Manufacturing PMI（季節調整済、index、>50 拡張）

方式:
    1. stats.gov.cn 英語版 PressRelease トップ HTML を 1 fetch。
    2. 本文中の anchor text に "Purchasing Managers' Index" を含むリンクを引き当てて、
       最新リリース 1 本のフル URL を解決（リリース ID は予測不能なため動的に引く）。
    3. リリース本文を fetch し、Table 1 "China's Manufacturing PMI and Sub-indexes (Seasonally Adjusted)"
       から PMI 列（headline）の 13 ヶ月分を抽出 → 共通スキーマ long DataFrame に変換。
    4. write_processed が key_cols=(date, indicator_id, region) で dedup するため、
       過去分との重複は無害。月次運用で時系列が自然に伸びる。

出力:
    - data/raw/nbs-pmi/{filename}_{YYYYMMDD}.html      （プレスリリース本体）
    - data/raw/nbs-pmi/index_{YYYYMMDD}.html           （引き当て元のインデックス）
    - data/processed/international/china-nbs-mfg-pmi.csv
    - data/processed/international/china-nbs-mfg-pmi.parquet
    - data/processed/international/china-nbs-mfg-pmi.metadata.json

参考:
    - https://www.stats.gov.cn/english/PressRelease/
    - ライセンス: NBS 英語版プレスリリース、出典明示で再利用可（nbs-terms）。
    - Caixin PMI は S&P Global proprietary のため不採用（L-063, 2026-05-27）。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from scripts.common.http import get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_nbs_pmi")

SOURCE_KEY = "nbs-pmi"
INDICATOR_ID = "china-nbs-mfg-pmi"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _decode(resp) -> str:
    return resp.content.decode(resp.apparent_encoding or "utf-8", errors="replace")


def find_latest_pmi_url(index_url: str, raw_dir: Path, today_tag: str) -> str:
    """インデックスから PMI プレスリリースの URL を引く。"""
    logger.info("GET index %s", index_url)
    r = get(index_url, timeout=60)
    r.raise_for_status()
    save_raw(r.content, raw_dir, f"index_{today_tag}.html")
    html = _decode(r)
    # 最初に登場する "Purchasing Managers' Index" アンカーが最新リリース。
    # NBS の正書法は U+2019 (’) と U+0027 (') が混在し得る — どちらも許容。
    pat = re.compile(
        r'<a[^>]+href="([^"]+)"[^>]*>[^<]*Purchasing\s+Managers[’\']\s*Index[^<]*</a>',
        re.IGNORECASE,
    )
    m = pat.search(html)
    if not m:
        raise RuntimeError("No 'Purchasing Managers' Index' link found on index page")
    href = m.group(1)
    full = urljoin(index_url, href)
    logger.info("resolved PMI release URL: %s", full)
    return full


def fetch_release(url: str, raw_dir: Path, today_tag: str) -> str:
    """プレスリリース HTML を fetch し、raw 保存して text を返す。"""
    logger.info("GET release %s", url)
    r = get(url, timeout=60)
    r.raise_for_status()
    # filename: t20260506_1963595.html → そのまま使う
    name = url.rsplit("/", 1)[-1]
    if not name.endswith(".html"):
        name = f"release_{today_tag}.html"
    else:
        name = name.replace(".html", f"_{today_tag}.html")
    save_raw(r.content, raw_dir, name)
    return _decode(r)


def parse_pmi_table(html: str) -> list[tuple[str, float]]:
    """
    Table 1 から (YYYY-MM-DD, PMI_value) のリストを抽出。

    フォーマット:
        ... Unit: % PMI Production Index ... 2025-April 49.0 49.8 ...
        May 49.5 ... June 49.7 ... ... December 50.1 ...
        2026-January 49.3 ... February 49.0 ... March 50.4 ... April 50.3 ...

    戻り値:
        [("2025-04-01", 49.0), ("2025-05-01", 49.5), ..., ("2026-04-01", 50.3)]
    """
    # HTML をプレーンテキスト化
    plain = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    plain = re.sub(r"<style.*?</style>", "", plain, flags=re.S)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()

    # Table 1 と Table 2 の間を抽出（Table 1 が manufacturing PMI、Table 2 は関連指標）
    seg = re.search(r"Table\s*1[^|]*?(.*?)Table\s*2", plain)
    if not seg:
        # 一部リリースで "Table 2" が "Table II" 表記の可能性に備える
        seg = re.search(
            r"Table\s*1\b(.*?)(?:Table\s*2\b|II\.\s*Non-manufacturing)",
            plain,
        )
    if not seg:
        raise RuntimeError("Table 1 section not found in release HTML")
    table_text = seg.group(1)

    # トークン化: 年マーカー / 月名 / 数値
    # 年マーカー例: "2025-April"
    # 月名: "January", "February", ..., "December"
    # 数値: 小数または整数
    token_pat = re.compile(
        r"(?P<year_month>\d{4})\s*-\s*(?P<ym_month>[A-Za-z]+)"
        r"|(?P<month>[A-Za-z]+)"
        r"|(?P<num>-?\d+(?:\.\d+)?)",
        re.UNICODE,
    )

    rows: list[tuple[str, float]] = []
    current_year: int | None = None
    pending_month: int | None = None
    values_after_month: list[float] = []

    def flush():
        nonlocal pending_month, values_after_month
        if pending_month is not None and current_year is not None and values_after_month:
            # PMI = 月名直後の 1 つ目の数値（headline）
            pmi = values_after_month[0]
            date_str = f"{current_year:04d}-{pending_month:02d}-01"
            rows.append((date_str, pmi))
        pending_month = None
        values_after_month = []

    for tok in token_pat.finditer(table_text):
        if tok.group("year_month"):
            flush()
            current_year = int(tok.group("year_month"))
            mname = tok.group("ym_month").lower()
            pending_month = MONTHS.get(mname)
        elif tok.group("month"):
            mname = tok.group("month").lower()
            if mname in MONTHS:
                flush()
                pending_month = MONTHS[mname]
            # 月名以外の英単語 (Unit, PMI, Production, Index 等) は無視
        elif tok.group("num"):
            try:
                v = float(tok.group("num"))
            except ValueError:
                continue
            if pending_month is not None:
                values_after_month.append(v)
    flush()

    # PMI の値域チェック（30-70 を妥当帯と想定）
    rows = [(d, v) for (d, v) in rows if 30.0 <= v <= 70.0]
    if not rows:
        raise RuntimeError("No PMI rows parsed from Table 1")
    return rows


def normalize_to_long(
    rows: list[tuple[str, float]],
    indicator_id: str,
    source_url: str,
) -> pd.DataFrame:
    cols = ["date", "indicator_id", "region", "value", "source_url"]
    if not rows:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(rows, columns=["date", "value"])
    out["indicator_id"] = indicator_id
    out["region"] = "cn"
    out["source_url"] = source_url
    out = out[cols].sort_values("date").reset_index(drop=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch China NBS Manufacturing PMI from stats.gov.cn English press release"
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="互換用。NBS リリースは常に直近 13 ヶ月のみを返す。",
    )
    args = parser.parse_args(argv)
    _ = args  # 現状 backfill による分岐は無い（dedup で安全）

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    index_url = source_cfg.get("index_url")
    if not index_url:
        logger.error("source_map.yaml の %s.index_url が未設定", SOURCE_KEY)
        return 2

    raw_dir = ROOT / "data" / "raw" / "nbs-pmi"
    processed_dir = ROOT / "data" / "processed" / "international"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    try:
        release_url = find_latest_pmi_url(index_url, raw_dir, today_tag)
        html = fetch_release(release_url, raw_dir, today_tag)
        rows = parse_pmi_table(html)
    except Exception as e:
        logger.exception("fetch failed: %s", e)
        append_log(log_dir, "fetch_nbs_pmi", "FAIL", str(e))
        return 1

    long_df = normalize_to_long(rows, INDICATOR_ID, release_url)
    if long_df.empty:
        logger.error("0 rows after parse — abort")
        append_log(log_dir, "fetch_nbs_pmi", "FAIL", "0 rows after parse")
        return 1

    write_processed(long_df, processed_dir, basename=INDICATOR_ID)
    write_metadata_for_indicator(processed_dir, source_cfg, INDICATOR_ID, long_df)

    summary = (
        f"series=1 rows={len(long_df)} "
        f"range={long_df['date'].min()}..{long_df['date'].max()} "
        f"latest={long_df.iloc[-1]['value']}"
    )
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_nbs_pmi", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
