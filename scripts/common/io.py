"""
共通 I/O ヘルパ。

責務:
- 共通スキーマ (date, indicator_id, region, value, source_url) の DataFrame を
  CSV と Parquet の両方に書き出す
- 追記モード（既存ファイルがあれば重複行を排除してマージ）
- 生ファイル（ダウンロードしたまま）の保存

依存:
- pandas
- pyarrow（Parquet 書き出しに必要）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 共通スキーマ（docs/schema.md と一致させる）
SCHEMA_COLUMNS = ["date", "indicator_id", "region", "value", "source_url"]


def ensure_dir(path: Path) -> Path:
    """ディレクトリを（必要なら作成して）返す。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_raw(
    content: bytes,
    dest_dir: Path,
    filename: str,
) -> Path:
    """
    一次ソースから取ってきたファイルをそのまま保存する。
    dest_dir は data/raw/{source}/ を想定。
    """
    ensure_dir(dest_dir)
    dest = dest_dir / filename
    dest.write_bytes(content)
    logger.info("saved raw: %s (%d bytes)", dest, len(content))
    return dest


def validate_schema(df: pd.DataFrame) -> None:
    """共通スキーマに沿っているか検証。足りない列があれば ValueError。"""
    missing = [c for c in SCHEMA_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")


def write_processed(
    df: pd.DataFrame,
    dest_dir: Path,
    basename: str,
    *,
    key_cols: Iterable[str] = ("date", "indicator_id", "region"),
) -> tuple[Path, Path]:
    """
    処理済みデータを CSV と Parquet で書き出す。
    既存ファイルがあれば読み込んでから `key_cols` でユニーク化してマージする（append-safe）。

    Returns:
        (csv_path, parquet_path)
    """
    validate_schema(df)
    # 列順を揃える
    df = df[SCHEMA_COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    ensure_dir(dest_dir)
    csv_path = dest_dir / f"{basename}.csv"
    parquet_path = dest_dir / f"{basename}.parquet"

    # 既存があればマージ
    if csv_path.exists():
        existing = pd.read_csv(csv_path, dtype={"date": str})
        validate_schema(existing)
        merged = pd.concat([existing, df], ignore_index=True)
    else:
        merged = df

    merged = merged.drop_duplicates(subset=list(key_cols), keep="last")
    merged = merged.sort_values(list(key_cols)).reset_index(drop=True)

    merged.to_csv(csv_path, index=False, encoding="utf-8")
    try:
        merged.to_parquet(parquet_path, index=False)
    except Exception as e:
        # pyarrow がインストールされていない等の場合は CSV だけでも残す
        logger.warning("parquet write failed (%s) — CSV only", e)

    logger.info(
        "wrote processed: %s (%d rows, range=%s..%s)",
        csv_path, len(merged),
        merged["date"].min() if len(merged) else "-",
        merged["date"].max() if len(merged) else "-",
    )
    return csv_path, parquet_path


def append_log(
    log_dir: Path,
    script_name: str,
    status: str,
    message: str = "",
) -> None:
    """data/_logs/ に 1 行追記する。"""
    ensure_dir(log_dir)
    log_path = log_dir / f"{script_name}.log"
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{ts}\t{status}\t{message}\n")
