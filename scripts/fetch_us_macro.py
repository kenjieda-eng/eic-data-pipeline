"""
米マクロ 3 系列（FRED 経由）の取得スクリプト。

対象（Phase 3-B 第 2 弾、source_map.yaml の `fred-macro` セクション）:
    - us-cpi-yoy            : CPIAUCSL の 12 ヶ月前比 % を算出
    - us-fed-funds-rate     : FEDFUNDS（月次平均、実効レート）
    - us-industrial-production : INDPRO（Index 2017=100）

方式:
    1. FRED API（公式、API キー必須・無料、120 req/min）を優先。
       環境変数 `FRED_API_KEY` を python-dotenv 経由で .env から読み込む。
    2. キー未設定時は認証不要 CSV エンドポイント（fredgraph.csv?id=...）にフォールバック。

出力:
    - data/raw/fred-macro/{fred_id}_{YYYYMMDD}.{json|csv}    （生レス）
    - data/processed/macro/{indicator_id}.csv                 （共通スキーマ long 形式）
    - data/processed/macro/{indicator_id}.parquet
    - data/processed/macro/{indicator_id}.metadata.json       （D-011）

参考:
    - https://fred.stlouisfed.org/docs/api/fred/series_observations.html
    - ライセンス: 連邦政府公表データ、public domain（17 U.S.C. § 105）。
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from scripts.common.http import get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

# .env 読み込み（GitHub Actions では env: で渡される。ローカル開発時のみ有効）
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=ROOT / ".env")
except ImportError:
    pass  # python-dotenv 未インストール時は env 直読みのみ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_us_macro")

SOURCE_KEY = "fred-macro"


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_api_key() -> str:
    """環境変数 FRED_API_KEY を読む。プレースホルダや空は無効扱い。"""
    key = (os.environ.get("FRED_API_KEY") or "").strip()
    if not key or key.lower() in {"your_api_key_here", "changeme", "todo"}:
        return ""
    return key


def fetch_via_api(
    api_base: str,
    fred_id: str,
    api_key: str,
    raw_dir: Path,
    today_tag: str,
) -> pd.DataFrame:
    """FRED API で観測データを JSON で取得。"""
    params = {
        "series_id": fred_id,
        "api_key": api_key,
        "file_type": "json",
    }
    logger.info("API GET %s series_id=%s", api_base, fred_id)
    r = get(api_base, params=params, timeout=60)
    r.raise_for_status()

    save_raw(r.content, raw_dir, f"{fred_id}_{today_tag}.json")

    payload = r.json() or {}
    obs = payload.get("observations") or []
    rows: list[tuple[str, float]] = []
    for o in obs:
        v = o.get("value")
        d = o.get("date")
        if not d or v is None or v == "." or v == "":
            continue
        try:
            rows.append((d, float(v)))
        except (TypeError, ValueError):
            continue
    if not rows:
        logger.warning("API returned 0 usable rows for %s", fred_id)
        return pd.DataFrame(columns=["_iso_date", "value"])

    df = pd.DataFrame(rows, columns=["_iso_date", "value"])
    df["_iso_date"] = pd.to_datetime(df["_iso_date"], errors="coerce")
    df = df.dropna(subset=["_iso_date"]).sort_values("_iso_date").reset_index(drop=True)
    return df


def fetch_via_csv(
    csv_base: str,
    fred_id: str,
    raw_dir: Path,
    today_tag: str,
) -> pd.DataFrame:
    """フォールバック: 認証不要 CSV エンドポイントから取得。"""
    url = f"{csv_base}?id={fred_id}"
    logger.info("CSV GET %s", url)
    r = get(url, timeout=60)
    r.raise_for_status()

    save_raw(r.content, raw_dir, f"{fred_id}_{today_tag}.csv")

    text = r.content.decode("utf-8", errors="replace")
    df = pd.read_csv(io.StringIO(text))
    if df.shape[1] < 2:
        logger.warning("CSV has unexpected shape for %s: %s", fred_id, df.shape)
        return pd.DataFrame(columns=["_iso_date", "value"])

    # 列名は通常 ["DATE", "<FRED_ID>"]（古い変種は ["observation_date", ...]）
    date_col = df.columns[0]
    val_col = df.columns[1]
    df = df.rename(columns={date_col: "_iso_date", val_col: "value"})
    df["_iso_date"] = pd.to_datetime(df["_iso_date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["_iso_date", "value"]).sort_values("_iso_date").reset_index(drop=True)
    return df


def fetch_series_raw(
    source_cfg: dict,
    fred_id: str,
    raw_dir: Path,
    today_tag: str,
) -> pd.DataFrame:
    """API 優先、失敗 or キー未設定なら CSV にフォールバック。"""
    api_key = _get_api_key()
    if api_key:
        try:
            df = fetch_via_api(source_cfg["api_base"], fred_id, api_key, raw_dir, today_tag)
            if not df.empty:
                return df
            logger.warning("API returned empty for %s — falling back to CSV", fred_id)
        except Exception as e:
            logger.warning("API failed for %s (%s) — falling back to CSV", fred_id, e)
    return fetch_via_csv(source_cfg["csv_base"], fred_id, raw_dir, today_tag)


def transform_yoy_pct(df: pd.DataFrame) -> pd.DataFrame:
    """月次 raw level → 12 ヶ月前比 %（端数 4 桁）。先頭 12 ヶ月は欠損で落ちる。"""
    if df.empty:
        return df
    s = df.set_index("_iso_date")["value"].sort_index()
    yoy = (s / s.shift(12) - 1.0) * 100.0
    yoy = yoy.dropna()
    if yoy.empty:
        return pd.DataFrame(columns=["_iso_date", "value"])
    out = yoy.reset_index()
    out.columns = ["_iso_date", "value"]
    out["value"] = out["value"].round(4)
    return out


TRANSFORMS = {
    "level": lambda df: df.assign(value=lambda d: d["value"]).copy(),
    "yoy_pct": transform_yoy_pct,
}


def normalize_to_long(
    df: pd.DataFrame,
    indicator_id: str,
    source_url: str,
) -> pd.DataFrame:
    """共通スキーマ（date, indicator_id, region, value, source_url）に変換。"""
    cols = ["date", "indicator_id", "region", "value", "source_url"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    out = df.copy()
    out["date"] = out["_iso_date"].dt.strftime("%Y-%m-%d")
    out["indicator_id"] = indicator_id
    out["region"] = "us"
    out["source_url"] = source_url
    out = out[cols].sort_values("date").reset_index(drop=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch US macro series from FRED (3 series: CPI YoY / Fed Funds / Industrial Production)"
    )
    parser.add_argument(
        "--series", type=str, default=None,
        help="カンマ区切りで indicator_id を絞る（例: us-cpi-yoy,us-fed-funds-rate）",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="全期間取得（FRED 側が常に全量返すため通常モードと同義、互換用）",
    )
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    indicators_cfg: dict = source_cfg.get("indicators") or {}
    if not indicators_cfg:
        logger.error("source_map.yaml の %s.indicators が空です", SOURCE_KEY)
        return 2

    if args.series:
        wanted = {s.strip() for s in args.series.split(",") if s.strip()}
        indicators_cfg = {k: v for k, v in indicators_cfg.items() if k in wanted}
        if not indicators_cfg:
            logger.error("--series で指定された ID が 1 つも該当しません: %s", args.series)
            return 2

    raw_dir = ROOT / "data" / "raw" / "fred-macro"
    processed_dir = ROOT / "data" / "processed" / "macro"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_tag = now_jst.strftime("%Y%m%d")

    api_key_present = bool(_get_api_key())
    logger.info("FRED API mode: %s", "API key" if api_key_present else "CSV fallback (no key)")

    written: list[str] = []
    total_rows = 0
    for indicator_id, ind_cfg in indicators_cfg.items():
        fred_id = ind_cfg.get("fred_series_id")
        transform = ind_cfg.get("transform", "level")
        if not fred_id:
            logger.warning("%s: fred_series_id が未定義 — skip", indicator_id)
            continue
        if transform not in TRANSFORMS:
            logger.warning("%s: 未対応 transform '%s' — skip", indicator_id, transform)
            continue

        try:
            raw_df = fetch_series_raw(source_cfg, fred_id, raw_dir, today_tag)
        except Exception as e:
            logger.exception("%s: fetch failed: %s", indicator_id, e)
            append_log(log_dir, "fetch_us_macro", "WARN", f"{indicator_id} fetch failed: {e}")
            continue

        if raw_df.empty:
            logger.warning("%s: raw is empty — skip", indicator_id)
            continue

        transformed = TRANSFORMS[transform](raw_df)
        if transformed.empty:
            logger.warning("%s: transform '%s' produced 0 rows — skip", indicator_id, transform)
            continue

        long_df = normalize_to_long(
            transformed, indicator_id,
            ind_cfg.get("citation_url") or source_cfg.get("source_url", ""),
        )
        if long_df.empty:
            logger.warning("%s: 0 rows after normalize — skip", indicator_id)
            continue

        write_processed(long_df, processed_dir, basename=indicator_id)
        write_metadata_for_indicator(processed_dir, source_cfg, indicator_id, long_df)
        written.append(indicator_id)
        total_rows += len(long_df)
        logger.info(
            "%s: %d rows (range=%s..%s, fred_id=%s, transform=%s)",
            indicator_id, len(long_df),
            long_df["date"].min(), long_df["date"].max(),
            fred_id, transform,
        )

    if not written:
        logger.error("no series produced any rows")
        append_log(log_dir, "fetch_us_macro", "FAIL", "no series produced rows")
        return 1

    summary = f"series={len(written)} rows={total_rows} api_key={'yes' if api_key_present else 'no'}"
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_us_macro", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
