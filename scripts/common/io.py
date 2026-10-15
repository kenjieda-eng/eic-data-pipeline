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

import glob
import logging
import re
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 共通スキーマ（docs/schema.md と一致させる）
SCHEMA_COLUMNS = ["date", "indicator_id", "region", "value", "source_url"]

# 取得日トークンつきの raw 名（'ember_monthly_20261015.csv'）。raw_ledger.py の DATE_TOKEN_RE と同じ規則
# （key = 日付を除いた名前 'ember_monthly.csv'）。save_raw の「前回と同じ中身なら書かない」に使う。
RAW_DATE_TOKEN_RE = re.compile(r"^(?P<stem>.+?)_(?P<date>20\d{6})(?P<ext>\.[A-Za-z0-9]+)$")


def ensure_dir(path: Path) -> Path:
    """ディレクトリを（必要なら作成して）返す。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def _raw_equal(old: bytes, new: bytes) -> bool:
    """raw_ledger.py の _sha256_file と同じ見方で比べる: 先頭 8KB に NUL が無ければテキストとして
    CR を無視する（Windows の checkout〈autocrlf〉で行末だけ CRLF になった raw を「同じ」とみなす）。
    バイナリ（xlsx・zip など）は 1 バイトでも違えば別物。
    """
    if old == new:
        return True
    if b"\x00" in old[:8192] or b"\x00" in new[:8192]:
        return False
    return old.replace(b"\r", b"") == new.replace(b"\r", b"")


def _latest_raw_same_key(dest_dir: Path, stem: str, ext: str, before: str) -> Optional[Path]:
    """同じ key（stem + ext）で、取得日トークンが before より前のファイルのうち最新のもの。"""
    best: Optional[Path] = None
    best_date = ""
    for p in dest_dir.glob(f"{glob.escape(stem)}_20??????{glob.escape(ext)}"):
        m = RAW_DATE_TOKEN_RE.match(p.name)
        if not m or m.group("stem") != stem or m.group("ext") != ext:
            continue
        d = m.group("date")
        if best_date < d < before:
            best, best_date = p, d
    return best


def save_raw(
    content: bytes,
    dest_dir: Path,
    filename: str,
) -> Path:
    """
    一次ソースから取ってきたファイルをそのまま保存する。
    dest_dir は data/raw/{source}/ を想定。

    名前に取得日トークン（_YYYYMMDD）があり、同じ key（日付を除いた名前）の直近のファイルと
    中身が同じなら（テキストは行末の CR を除いて比べる。raw 台帳と同じ）、新しいファイルは作らずに
    既存のパスを返す（2026-10-15:
    data/raw の 88% が前日と同じ中身の複製で、checkout が 10 GB を超えていたため）。
    raw_ledger.py は key ごとの最新ファイルの sha256 で判定するので、凍結・変化の判定は
    変わらない。ファイル名の日付は「その中身を最初に取った日」になる。
    同じ日の 2 回目（同名が既にある）と、日付トークンの無い名前（上書き型）は従来どおり上書きする。
    """
    ensure_dir(dest_dir)
    dest = dest_dir / filename
    m = RAW_DATE_TOKEN_RE.match(filename)
    if m and not dest.exists():
        prev = _latest_raw_same_key(dest_dir, m.group("stem"), m.group("ext"), m.group("date"))
        if prev is not None and _raw_equal(prev.read_bytes(), content):
            logger.info("raw unchanged since %s: %s not written (%d bytes)",
                        prev.name, filename, len(content))
            return prev
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
    replace: bool = False,
) -> tuple[Path, Path]:
    """
    処理済みデータを CSV と Parquet で書き出す。
    既存ファイルがあれば読み込んでから `key_cols` でユニーク化してマージする（append-safe）。

    Args:
        replace: True なら既存ファイルとマージせず `df` だけで**全置換**する。
            既定 False（＝従来どおりの追記マージ）。

            全置換が必要なのは「1 ファイルに全期間が入っており、かつ新版が過去分も
            遡及改定するソース」。追記マージだと旧版の値が残って新旧混在の時系列に
            なってしまう。GIO 温室効果ガス（fetch_gio.py）は利用規約自体が
            「公開日の異なる版を組み合わせての使用は避けてください」と定めているため
            全置換が唯一正しい書き方になる。
            ★ 呼び出し側は「書き出す前に全系列そろっているか」を検証すること。
              部分的な取得結果で replace=True すると既存データを削ってしまう。

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

    # 既存があればマージ（replace=True のときは既存を読まずに丸ごと差し替える）
    if csv_path.exists() and not replace:
        # ★ float_precision="round_trip" は必須（2026-09-22）。
        # pandas の既定 float パーサは**正確に丸めない**ため、CSV に書かれた
        # "14.462708333333333"（真値に最も近い double の最短 repr）を読むと 1 ULP ずれた
        # double になり、書き戻すと "14.462708333333332" になる。崩れた形は固定点なので
        # 以後は安定するが、fetcher が新しく計算して書いた値は翌日のマージで必ず 1 ULP
        # 崩れる。実害: jepx-spot 10 系列で backfill（--all）のたびに 8,726 行（16.5%）が
        # 書き換わり、翌日の nightly で戻る、を繰り返していた（2026-09-21〜22 に実測）。
        # "round_trip" は Python の float() と同じ正確なパーサを使うので往復が byte 一致する。
        existing = pd.read_csv(csv_path, dtype={"date": str}, float_precision="round_trip")
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
        "wrote processed%s: %s (%d rows, range=%s..%s)",
        " (replace)" if replace else "",
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
