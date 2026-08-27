"""
JEPX 非化石価値取引市場 約定価格・約定量の取得スクリプト（regulation ドメイン、6 系列）。

L-063 事前確認: energy-data-platform/docs/l063-check-energy-gx-2026-08-07.md §A-1（🟢 GO）

取得経路（レポート A-1 §1 で実測特定した公式エンドポイント）:
    1) 収録年度レンジ
       GET https://www.jepx.jp/js/get_graph_year.php?dir=nf_summary
       → 応答本文 "2025,2017"（= 最新年度, 最古年度 のカンマ区切り）
    2) 年度別オークション結果
       GET https://www.jepx.jp/js/csv_read.php?dir=nf_summary&file=nf_summary_{FY}.csv

    どちらも市場ページ (market_page) を先に GET してセッションを確立し、
    Referer を付ける。HTTP 作法は fetch_jepx.py と同じ common/http の
    make_session / session_get / リトライ機構をそのまま流用する（新規の作法を作らない）。

    ★ 取得範囲は 1) の応答から決める。年度をハードコードしない。
      （Pink Sheet 型サイレント停滞の予防: 年度ローテーションを公式レスポンスで検知できる）

出力:
- data/raw/jepx-nonfossil/nf_summary_{FY}.csv           （生ファイル）
- data/processed/regulation/nonfossil-cert-*.csv        （共通スキーマ long 形式）
- data/processed/regulation/nonfossil-cert-*.parquet
- data/processed/regulation/nonfossil-cert-*.metadata.json  （D-011）

系列（6 系列 = レポート A-1 §8 の基本案）:
    価格 3 (¥/kWh):  nonfossil-cert-fit-price
                     nonfossil-cert-nonfit-re-price
                     nonfossil-cert-nonfit-price
    約定量 3 (kWh):  nonfossil-cert-fit-volume
                     nonfossil-cert-nonfit-re-volume
                     nonfossil-cert-nonfit-volume

    レポートが「密度版 +3」として挙げた入札倍率（買い入札量 ÷ 約定総量）は採用しない。
    実データを全 9 年度確認したところ FIT は 買い入札量 == 約定総量 が恒常的に成立し
    （売り超過で買い注文が全量約定するため）、倍率が常に 1.000 の定数系列になる。
    定数系列は catalog のノイズにしかならないので基本案の 6 系列を採る。

実データ調査で確定した仕様（全 FY2017-2025 の 9 ファイルを走査して確認、L-062）:
- ヘッダは 9 年度すべて同一（12 列）。商品は 3 種で固定・表記ゆれなし。
- 開催回は {1,2,3,4} だが **FY2017 だけ "通年"**（年 1 回開催だった時期）。数値前提にしない。
- 未開催・データなしのセルは空文字ではなく **"-"（ハイフン）**。
  「開催されなかった (年度, 開催回, 商品)」は行自体は存在し、全列が "-" になる。
  → 約定日が "-" の行はタイムライン上に置けないのでスキップする。
- 非FIT 2 種は FY2020 の第 2 回が初回（FY2017-2019 と FY2020 第 1 回は全て "-"）。
- **FY2021 の第 1 回だけ FIT が "-"**。欠落は系列の先頭だけでなく途中にも出る。
  → 「最初の N 年だけ落とす」ような実装にはしない。行単位で判定する。
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml

# 連続取得時、JEPX サーバに配慮して年度間に挟むスリープ秒数。
# 1 ファイル約 1.2KB・年 4 回更新のソースなので負荷は実質ゼロだが、
# fetch_jepx.py と同じ「毎リクエスト待つ」作法に揃える。
SLEEP_BETWEEN_YEARS = 2

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
logger = logging.getLogger("fetch_nonfossil")

SOURCE_KEY = "jepx-nonfossil"

# 元 CSV の商品名 → indicator_id の中置スラグ
PRODUCT_SLUG = {
    "FIT": "fit",
    "非FIT(再エネ指定)": "nonfit-re",
    "非FIT(再エネ指定なし)": "nonfit",
}

# 元 CSV の列名 → (id サフィックス, 単位ラベル)
METRIC_COLUMNS = {
    "約定価格(円/kWh)": "price",
    "約定総量(kWh)": "volume",
}

# 未取得・未開催セルの表現（空文字ではなくハイフン）
NULL_TOKENS = {"-", "", "－", "ー"}

DATE_COL = "約定日"
PRODUCT_COL = "商品"
ROUND_COL = "開催回"

FY_MIN_FALLBACK = 2017  # 年度レンジ API が落ちたときの下限（実測の最古年度）


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _current_fiscal_year_jst() -> int:
    """JST の現在日から年度を返す（4 月始まり）。"""
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    return now.year if now.month >= 4 else now.year - 1


def fetch_year_range(session, url: str, referer: str) -> tuple[int, int] | None:
    """
    収録年度レンジ API を叩いて (oldest_fy, latest_fy) を返す。
    応答本文は "2025,2017"（最新, 最古）のカンマ区切り。パースできなければ None。
    """
    logger.info("GET %s", url)
    resp = session_get(session, url, headers={"Referer": referer})
    if resp.status_code >= 400:
        logger.warning("year-range API HTTP %d", resp.status_code)
        return None

    body = resp.text.strip()
    parts = [p.strip() for p in body.split(",") if p.strip()]
    years = [int(p) for p in parts if p.isdigit() and len(p) == 4]
    if len(years) < 2:
        logger.warning("year-range API returned unparseable body: %r", body)
        return None

    latest, oldest = max(years), min(years)
    logger.info(
        "year-range API: raw=%r → oldest_fy=%d latest_fy=%d (%d fiscal years)",
        body, oldest, latest, latest - oldest + 1,
    )
    return oldest, latest


def download_fiscal_year(
    session,
    csv_url_template: str,
    fiscal_year: int,
    referer: str,
    raw_dir: Path,
) -> list[dict] | None:
    """1 年度分の CSV を取得して dict のリストで返す。失敗時 None。"""
    url = csv_url_template.format(fy=fiscal_year)
    logger.info("GET %s", url)
    resp = session_get(session, url, headers={"Referer": referer})

    if resp.status_code >= 400:
        logger.warning("FY%d HTTP %d — skipping", fiscal_year, resp.status_code)
        return None

    content = resp.content
    head = content[:200].lower()
    if b"<html" in head or b"<!doctype" in head or len(content) < 80:
        # Content-Type は text/html で返ってくるので中身で判定する（ヘッダを信用しない）
        logger.warning(
            "FY%d non-CSV response: size=%d head=%r",
            fiscal_year, len(content), content[:120],
        )
        return None

    filename = f"nf_summary_{fiscal_year}.csv"
    save_raw(content, raw_dir, filename)

    # 実測では UTF-8（BOM なし）。cp932 は後方互換のフォールバックとしてのみ残す。
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError(f"could not decode nonfossil CSV for FY={fiscal_year}")

    rows = list(csv.DictReader(io.StringIO(text)))
    logger.info("parsed FY%d rows=%d", fiscal_year, len(rows))
    return rows


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _is_null(value: str | None) -> bool:
    return _clean(value) in NULL_TOKENS


def _parse_settlement_date(value: str) -> str | None:
    """約定日 "2026/5/20" → "2026-05-20"。パースできなければ None。"""
    raw = _clean(value)
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize(
    raw_rows: Iterable[dict],
    fiscal_year: int,
    source_url: str,
) -> tuple[pd.DataFrame, list[str]]:
    """
    生 CSV の行を共通スキーマ long 形式に変換する。

    Returns:
        (DataFrame, skipped_reasons)
        skipped_reasons は「なぜその行を落としたか」の人間向けメモ（ログ用）。
    """
    rows: list[dict] = []
    skipped: list[str] = []

    for raw in raw_rows:
        product = _clean(raw.get(PRODUCT_COL))
        round_label = _clean(raw.get(ROUND_COL))  # {1,2,3,4} or "通年"（FY2017）

        slug = PRODUCT_SLUG.get(product)
        if slug is None:
            # 商品名が増えた/変わったら黙って落とさず気付けるようにする
            skipped.append(f"FY{fiscal_year} 第{round_label}回: 未知の商品名 {product!r}")
            continue

        if _is_null(raw.get(DATE_COL)):
            # 未開催（該当 (年度, 開催回, 商品) が存在しない）。正常系。
            continue

        date = _parse_settlement_date(raw.get(DATE_COL, ""))
        if date is None:
            skipped.append(
                f"FY{fiscal_year} 第{round_label}回 {product}: "
                f"約定日をパースできない {raw.get(DATE_COL)!r}"
            )
            continue

        for column, metric in METRIC_COLUMNS.items():
            cell = raw.get(column)
            if _is_null(cell):
                continue
            try:
                value = float(_clean(cell).replace(",", ""))
            except ValueError:
                skipped.append(
                    f"FY{fiscal_year} 第{round_label}回 {product} {column}: "
                    f"数値化できない {cell!r}"
                )
                continue
            rows.append({
                "date": date,
                "indicator_id": f"nonfossil-cert-{slug}-{metric}",
                "region": "jp",
                "value": value,
                "source_url": source_url,
            })

    return pd.DataFrame(rows), skipped


def check_sanity_bands(df: pd.DataFrame, source_cfg: dict) -> list[str]:
    """
    価格系列が制度上の下限〜上限の枠内かを **書き出し前に** 検査する。

    目的は「列を取り違えて約定量を価格として書き込む」類のパースバグを、
    ゴミが data/processed に着地する前に落とすこと（約定量は 1e8 オーダーなので
    価格の枠を必ず突き抜ける）。

    枠は source_map.yaml の indicators.{id}.sanity_min / sanity_max で持つ
    （制度改正で枠が動いてもコード修正なしで追随できるようにするため）。

    判定の粒度:
      - **最新観測** が枠外 → errors（呼び出し側で書き出しを中止させる）
      - 過去観測が枠外     → warning ログのみ。下限・上限は制度改正で変わっており、
        現行の枠を過去に遡って適用するのは誤り（例: FIT の最低価格は
        FY2017-2020 が 1.3 円、FY2021-2022 が 0.3 円台、FY2023 以降が 0.4 円）。
    """
    errors: list[str] = []
    indicators = source_cfg.get("indicators") or {}

    for indicator_id, group in df.groupby("indicator_id"):
        cfg = indicators.get(str(indicator_id)) or {}
        lo, hi = cfg.get("sanity_min"), cfg.get("sanity_max")
        if lo is None or hi is None:
            continue

        ordered = group.sort_values("date")
        out_of_band = ordered[(ordered["value"] < lo) | (ordered["value"] > hi)]
        latest = ordered.iloc[-1]

        if not (lo <= latest["value"] <= hi):
            errors.append(
                f"{indicator_id}: 最新観測 {latest['date']} = {latest['value']} が "
                f"想定枠 [{lo}, {hi}] の外。パース列の取り違えを疑うこと"
            )
        elif len(out_of_band):
            logger.warning(
                "%s: 過去 %d 件が現行の枠 [%s, %s] 外（制度改正による下限/上限の変更。"
                "最新 %s = %s は枠内なので継続）: %s",
                indicator_id, len(out_of_band), lo, hi,
                latest["date"], latest["value"],
                ", ".join(
                    f"{r.date}={r.value}" for r in out_of_band.itertuples()
                ),
            )
        else:
            logger.info(
                "%s: 最新 %s = %s（枠 [%s, %s] 内）",
                indicator_id, latest["date"], latest["value"], lo, hi,
            )

    return errors


def pick_fiscal_years(args, oldest: int, latest: int) -> list[int]:
    available = list(range(oldest, latest + 1))
    if args.year:
        return [args.year] if args.year in available else []
    if args.all:
        return available
    return available[-args.years:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch JEPX non-fossil certificate auction prices & volumes",
    )
    parser.add_argument(
        "--years", type=int, default=2,
        help="直近 N 年度を取得（既定 2: 当年度 + 前年度。前年度の遡及訂正を拾う）",
    )
    parser.add_argument("--year", type=int, default=None, help="単一年度のみ取得")
    parser.add_argument(
        "--all", action="store_true",
        help="年度レンジ API が返す全年度を取得（初回バックフィル用）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    market_page: str = source_cfg["market_page"]
    year_range_url: str = source_cfg["year_range_url"]
    csv_url_template: str = source_cfg["csv_url_template"]

    raw_dir = ROOT / "data" / "raw" / "jepx-nonfossil"
    processed_dir = ROOT / "data" / "processed" / "regulation"
    log_dir = ROOT / "data" / "_logs"

    session = make_session()

    # 1) 市場ページを GET してセッションを確立（fetch_jepx.py と同じ作法）
    logger.info("priming session via %s", market_page)
    resp = session_get(session, market_page)
    resp.raise_for_status()

    # 2) 収録年度レンジを API から取得（★ ハードコードしない）
    year_range = None
    try:
        year_range = fetch_year_range(session, year_range_url, referer=market_page)
    except Exception as e:
        logger.warning("year-range API failed (%s)", e)

    if year_range is None:
        oldest, latest = FY_MIN_FALLBACK, _current_fiscal_year_jst()
        logger.warning(
            "falling back to enumeration: oldest_fy=%d latest_fy=%d "
            "（年度レンジ API が使えないので推定。ローテーション検知が効かない点に注意）",
            oldest, latest,
        )
    else:
        oldest, latest = year_range

    logger.info("収録年度レンジ: FY%d〜FY%d（最新年度 = FY%d）", oldest, latest, latest)

    target_years = pick_fiscal_years(args, oldest, latest)
    if not target_years:
        msg = f"no target fiscal years after filtering. available=FY{oldest}..FY{latest}"
        logger.error(msg)
        append_log(log_dir, "fetch_nonfossil", "FAIL", msg)
        return 1
    logger.info("target fiscal years: %s", target_years)

    frames: list[pd.DataFrame] = []
    fetched: list[int] = []
    failed: list[int] = []
    all_skipped: list[str] = []

    for i, fiscal_year in enumerate(target_years):
        if i > 0 and SLEEP_BETWEEN_YEARS > 0:
            logger.info("sleeping %ds before next fiscal year (server courtesy)",
                        SLEEP_BETWEEN_YEARS)
            time.sleep(SLEEP_BETWEEN_YEARS)
        try:
            raw_rows = download_fiscal_year(
                session, csv_url_template, fiscal_year,
                referer=market_page, raw_dir=raw_dir,
            )
        except Exception as e:
            logger.exception("fetch failed FY=%d: %s", fiscal_year, e)
            failed.append(fiscal_year)
            continue
        if raw_rows is None:
            failed.append(fiscal_year)
            continue

        source_url = csv_url_template.format(fy=fiscal_year)
        try:
            norm, skipped = normalize(raw_rows, fiscal_year, source_url=source_url)
        except Exception as e:
            logger.exception("normalize failed FY=%d: %s", fiscal_year, e)
            failed.append(fiscal_year)
            continue

        all_skipped.extend(skipped)
        if norm.empty:
            logger.info("FY%d: 有効な約定行なし（全て未開催）", fiscal_year)
        else:
            frames.append(norm)
        fetched.append(fiscal_year)

    for note in all_skipped:
        logger.warning("skipped: %s", note)

    if not frames:
        msg = f"no data fetched (fetched={fetched} failed={failed})"
        logger.error(msg)
        append_log(log_dir, "fetch_nonfossil", "FAIL", msg)
        return 1

    merged = pd.concat(frames, ignore_index=True)

    # 3) 書き出し前のサニティチェック（ゴミを着地させない）
    errors = check_sanity_bands(merged, source_cfg)
    if errors:
        for e in errors:
            logger.error("sanity check failed: %s", e)
        msg = "sanity band violation (nothing written): " + " / ".join(errors)
        append_log(log_dir, "fetch_nonfossil", "FAIL", msg)
        return 1

    # 4) 系列ごとに CSV / Parquet / metadata.json を書き出す
    for indicator_id, group in merged.groupby("indicator_id"):
        write_processed(group, processed_dir, basename=str(indicator_id))
        write_metadata_for_indicator(
            processed_dir, source_cfg, str(indicator_id), group,
        )

    # D-020④: フェッチ成功範囲で行が来なかった indicator も metadata を書き直す
    # （updated_at = 生存信号）。非化石価値は年度単位で fetch するため、failed が
    # 空のときだけ refresh する。未開催回で行ゼロになる系列があるのが本命ケース。
    meta_refreshed: list[str] = []
    meta_skipped: list[str] = []
    if not failed:
        expected_ids = set(source_cfg.get("indicator_ids") or [])
        meta_refreshed, meta_skipped = write_metadata_for_expected_indicators(
            processed_dir, source_cfg, sorted(expected_ids - set(merged["indicator_id"].astype(str)))
        )
    else:
        logger.warning(
            "metadata refresh skipped: failed あり "
            "— 失敗範囲の updated_at は進めない（D-020 §2.4 軸2 の故障隠蔽を防ぐ）"
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
        f"{iid}={len(g)}" for iid, g in sorted(merged.groupby("indicator_id"))
    )
    summary = (
        f"fiscal_years={fetched} latest_fy={latest} rows={len(merged)} "
        f"range={merged['date'].min()}..{merged['date'].max()} "
        f"series=[{per_series}] failed={failed} "
        f"metadata_refreshed={len(meta_refreshed)}"
    )
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_nonfossil", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
