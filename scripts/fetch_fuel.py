"""
燃料価格の取得スクリプト（World Bank Pink Sheet, 月次）。

方式:
    GET https://thedocs.worldbank.org/.../CMO-Historical-Data-Monthly.xlsx
    Excel（シート "Monthly Prices"）に 1960-01 からの月次時系列が格納されている。
    列ヘッダが 2 段、単位行が 1 行、データ行は "YYYYMmm" 形式（例: "1960M01"）。

対象:
    7 系列（日本 LNG / 米国 Henry Hub / 欧州 TTF / Brent / Dubai / WTI / 豪州石炭）
    将来、米国石炭・南ア石炭なども source_map.yaml に足すだけで追加可能。

出力:
    - data/raw/fuel/CMO-Historical-Data-Monthly_{YYYYMMDD}.xlsx  （生ファイル）
    - data/processed/fuel/{indicator_id}.csv                     （共通スキーマ long 形式）
    - data/processed/fuel/{indicator_id}.parquet

参考:
    World Bank Commodity Markets: https://www.worldbank.org/en/research/commodity-markets
    ライセンス: CC BY 4.0 (World Bank Open Data Terms)
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.http import get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_fuel")

SOURCE_KEY = "wb-pink-sheet"

# "1960M01" / "2024M12" 形式の日付をパースするための正規表現
DATE_PATTERN = re.compile(r"^(\d{4})M(\d{1,2})$")


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_month_code(code: str) -> str | None:
    """'1960M01' → '1960-01-01' のように月初日に正規化。不正値は None。"""
    if not isinstance(code, str):
        return None
    m = DATE_PATTERN.match(code.strip())
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2))
    if not (1 <= month <= 12):
        return None
    try:
        return datetime(year, month, 1).strftime("%Y-%m-%d")
    except ValueError:
        return None


def find_column(headers: list[str], include_all: list[str], exclude: list[str] | None = None) -> int | None:
    """
    列ヘッダ配列から、指定したキーワード全部を含み、exclude のいずれも含まない列の index を返す。
    大文字小文字は無視。見つからなければ None。

    World Bank の列名は年度で揺れる可能性があるため部分一致で引く。
    例: include_all=['Natural gas', 'U.S.'] で "Natural gas, U.S." を拾う。
    """
    exclude = exclude or []
    lowered_includes = [s.lower() for s in include_all]
    lowered_excludes = [s.lower() for s in exclude]
    for i, h in enumerate(headers):
        if not isinstance(h, str):
            continue
        hl = h.lower()
        if all(inc in hl for inc in lowered_includes) and not any(exc in hl for exc in lowered_excludes):
            return i
    return None


def download_xlsx(url: str) -> bytes:
    """Pink Sheet XLSX を DL してバイト列で返す。"""
    logger.info("GET %s", url)
    r = get(url, timeout=60)
    r.raise_for_status()
    if len(r.content) < 50_000:
        raise RuntimeError(
            f"downloaded xlsx is suspiciously small ({len(r.content)} bytes); "
            f"content preview: {r.content[:200]!r}"
        )
    logger.info("downloaded %d bytes", len(r.content))
    return r.content


def parse_monthly_prices(xlsx_bytes: bytes, sheet_name: str = "Monthly Prices") -> pd.DataFrame:
    """
    Pink Sheet の "Monthly Prices" シートを読み、列 ["date_code", 品目1, 品目2, ...] の
    wide 形式 DataFrame を返す（値は数値、欠測は NaN）。

    シート構造（典型例）:
      行 1-3: タイトル / 説明
      行 4:   カテゴリ（"Crude oil" / "Natural gas" / ...）
      行 5:   品目ヘッダ（"Brent" / "U.S." / "Japan, LNG" / ...）
      行 6:   単位（"$/bbl" / "$/mmbtu" / ...）
      行 7+:  データ（列 A = "1960M01" 形式）

    実装方針: まず全行を header なしで読み、最初に "YYYYMmm" パターンがマッチする行を
    データ先頭と判定。その直前 2 行を合成してヘッダとする。
    """
    df_raw = pd.read_excel(
        BytesIO(xlsx_bytes),
        sheet_name=sheet_name,
        header=None,
        engine="openpyxl",
    )

    # データ先頭行を探す
    # .astype(str).tolist() は pandas のバージョンによって元の型（float NaN など）が
    # 漏れることがあるため、要素ごとに str() で強制変換する。
    data_start = None
    for idx, v in enumerate(df_raw.iloc[:, 0].tolist()):
        v_str = str(v) if v is not None else ""
        if DATE_PATTERN.match(v_str.strip()):
            data_start = idx
            break
    if data_start is None:
        raise RuntimeError("could not locate date column start in 'Monthly Prices' sheet")

    logger.info("data rows start at index %d (first date=%s)", data_start, df_raw.iloc[data_start, 0])

    # ヘッダ行の合成: データ先頭より手前の行から、最も意味のある行を品目ヘッダとする。
    # 典型: 直前 2 行目がカテゴリ、直前 1 行目が品目名。2 行を連結して列名を作る。
    # astype(str) は tolist() で float NaN が漏れるケースがあるので、要素ごとに str() で強制。
    def _row_as_strs(row) -> list[str]:
        return [str(x) if x is not None else "" for x in row.tolist()]

    hdr_row_a = _row_as_strs(df_raw.iloc[max(0, data_start - 3)])
    hdr_row_b = _row_as_strs(df_raw.iloc[max(0, data_start - 2)])
    unit_row = _row_as_strs(df_raw.iloc[max(0, data_start - 1)])

    # 前方埋め: カテゴリ行は結合セルだったことがあるので空欄を左から埋める
    def _ffill(seq: list[str]) -> list[str]:
        out: list[str] = []
        last = ""
        for s in seq:
            s2 = str(s).strip()
            if s2 and s2.lower() != "nan":
                last = s2
                out.append(s2)
            else:
                out.append(last)
        return out

    hdr_a_f = _ffill(hdr_row_a)
    hdr_b_f = hdr_row_b

    combined: list[str] = []
    for a, b in zip(hdr_a_f, hdr_b_f):
        a = (a or "").strip()
        b = (b or "").strip()
        if b and b.lower() != "nan":
            if a and a.lower() not in b.lower():
                combined.append(f"{a}, {b}")
            else:
                combined.append(b)
        else:
            combined.append(a)

    # 単位行は参考情報としてログだけ残す
    logger.debug("units row sample: %s", unit_row[:10])

    # 実データ部分だけ切り出し
    data = df_raw.iloc[data_start:].copy()
    data.columns = ["date_code"] + combined[1:]
    # date_code が "YYYYMmm" にマッチしない行は捨てる
    mask = data["date_code"].astype(str).str.match(r"^\d{4}M\d{1,2}$", na=False)
    data = data[mask].reset_index(drop=True)

    logger.info("parsed %d monthly rows, %d commodity columns", len(data), len(combined) - 1)
    return data


def normalize_series(
    df_wide: pd.DataFrame,
    series_defs: list[dict],
    source_url: str,
) -> dict[str, pd.DataFrame]:
    """
    series_defs の各エントリについて wide → long に変換し、indicator_id ごとに DataFrame を返す。

    series_defs の各エントリ:
        { id: 'fuel-lng-jp-cif', match: ['Japan', 'LNG'], exclude: [], region: 'jp', ... }
    """
    headers = df_wide.columns.tolist()
    out: dict[str, pd.DataFrame] = {}

    for sd in series_defs:
        indicator_id = sd["id"]
        include = sd.get("match", [])
        exclude = sd.get("exclude", [])
        region = sd.get("region", "global")

        col_idx = find_column(headers, include, exclude)
        if col_idx is None:
            logger.warning(
                "series=%s: no matching column for include=%s exclude=%s — skip",
                indicator_id, include, exclude,
            )
            continue
        col_name = headers[col_idx]
        logger.info("series=%s ← column[%d] '%s'", indicator_id, col_idx, col_name)

        rows = []
        for _, rr in df_wide.iterrows():
            ymd = parse_month_code(str(rr["date_code"]))
            if ymd is None:
                continue
            raw = rr.iloc[col_idx]
            # NaN / 空 / "…" などを落とす
            if pd.isna(raw):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            rows.append({
                "date": ymd,
                "indicator_id": indicator_id,
                "region": region,
                "value": value,
                "source_url": source_url,
            })
        if not rows:
            logger.warning("series=%s: 0 rows after normalize — skip", indicator_id)
            continue
        out[indicator_id] = pd.DataFrame(rows)
        logger.info("series=%s: %d rows (range=%s..%s)",
                    indicator_id, len(rows),
                    out[indicator_id]["date"].min(),
                    out[indicator_id]["date"].max())

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch World Bank Pink Sheet monthly fuel prices")
    parser.add_argument(
        "--series",
        type=str,
        default=None,
        help="カンマ区切りで indicator_id を絞る（例: fuel-lng-jp-cif,fuel-crude-brent）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    url = source_cfg["xlsx_url"]
    fallback_urls: list[str] = source_cfg.get("fallback_urls", []) or []
    sheet_name: str = source_cfg.get("sheet_name", "Monthly Prices")
    series_defs: list[dict] = source_cfg["series"]

    # --series で絞り込み
    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}
        series_defs = [s for s in series_defs if s["id"] in wanted]
        if not series_defs:
            logger.error("--series で指定された ID が 1 つも該当しません: %s", args.series)
            return 2

    raw_dir = ROOT / "data" / "raw" / "fuel"
    processed_dir = ROOT / "data" / "processed" / "fuel"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    # DL（失敗したら fallback_urls を試す）
    xlsx_bytes: bytes | None = None
    tried_urls = [url] + fallback_urls
    last_err: Exception | None = None
    for try_url in tried_urls:
        try:
            xlsx_bytes = download_xlsx(try_url)
            url = try_url  # source_url として最後に成功した URL を使う
            break
        except Exception as e:
            logger.warning("download failed at %s: %s", try_url, e)
            last_err = e
            continue
    if xlsx_bytes is None:
        logger.error("all URLs failed: last=%s", last_err)
        append_log(log_dir, "fetch_fuel", "FAIL", f"download failed: {last_err}")
        return 1

    # 生ファイル保存
    save_raw(
        xlsx_bytes,
        raw_dir,
        f"CMO-Historical-Data-Monthly_{today_tag}.xlsx",
    )

    # パース
    try:
        df_wide = parse_monthly_prices(xlsx_bytes, sheet_name=sheet_name)
    except Exception as e:
        logger.exception("parse failed: %s", e)
        append_log(log_dir, "fetch_fuel", "FAIL", f"parse failed: {e}")
        return 1

    # 共通スキーマに正規化
    per_id = normalize_series(df_wide, series_defs, source_url=url)
    if not per_id:
        logger.error("no series produced any rows — likely column-name change in Pink Sheet")
        append_log(log_dir, "fetch_fuel", "FAIL", "no series produced rows")
        return 1

    # 書き出し
    written: list[str] = []
    total_rows = 0
    for indicator_id, df in per_id.items():
        write_processed(df, processed_dir, basename=indicator_id)
        written.append(indicator_id)
        total_rows += len(df)

    summary = f"series={len(written)} rows={total_rows}"
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_fuel", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
