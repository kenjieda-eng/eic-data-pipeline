"""#73（2026-11-04）: write_processed は CSV の中身（bytes）が既存と同じで parquet もあれば、CSV も parquet も
書き直さない（pandas・pyarrow の版が上がった日に、parquet 534 本が無改訂のまま書き換わっていた）。
parquet が無いとき・EIC_REWRITE_PROCESSED=1 のときは従来どおり両方書く。"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from scripts.common import io as cio

OLD_T = 1_000_000_000  # この mtime のままなら「書き直していない」


def df_of(v3: float) -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2026-01-01", "2026-02-01", "2026-03-01"],
        "indicator_id": ["t"] * 3,
        "region": ["jp"] * 3,
        "value": [1.5, 2.25, v3],
        "source_url": ["https://example.invalid/x"] * 3,
    })


def pin(*paths: Path) -> None:
    for p in paths:
        os.utime(p, (OLD_T, OLD_T))


def untouched(p: Path) -> bool:
    return int(p.stat().st_mtime) == OLD_T


@pytest.fixture(autouse=True)
def _no_rewrite_env(monkeypatch):
    monkeypatch.delenv("EIC_REWRITE_PROCESSED", raising=False)


@pytest.fixture
def written(tmp_path):
    csv_p, pq_p = cio.write_processed(df_of(3.0), tmp_path, "t")
    assert csv_p.exists() and pq_p.exists()
    return tmp_path, csv_p, pq_p


def test_same_content_not_rewritten(written):
    d, csv_p, pq_p = written
    csv0, pq0 = csv_p.read_bytes(), pq_p.read_bytes()
    pin(csv_p, pq_p)
    cio.write_processed(df_of(3.0), d, "t")
    assert untouched(csv_p) and untouched(pq_p)
    assert csv_p.read_bytes() == csv0 and pq_p.read_bytes() == pq0


def test_changed_value_rewrites_both(written):
    d, csv_p, pq_p = written
    pin(csv_p, pq_p)
    cio.write_processed(df_of(4.0), d, "t")
    assert not untouched(csv_p) and not untouched(pq_p)
    body = csv_p.read_bytes()
    assert b"4.0" in body and b"3.0" not in body
    back = pd.read_parquet(pq_p)
    assert len(back) == 3 and float(back["value"].iloc[2]) == 4.0


def test_missing_parquet_rewrites_both(written):
    d, csv_p, pq_p = written
    pq_p.unlink()
    pin(csv_p)
    cio.write_processed(df_of(3.0), d, "t")
    assert pq_p.exists() and not untouched(csv_p)


def test_env_forces_rewrite(written, monkeypatch):
    d, csv_p, pq_p = written
    pin(csv_p, pq_p)
    monkeypatch.setenv("EIC_REWRITE_PROCESSED", "1")
    cio.write_processed(df_of(3.0), d, "t")
    assert not untouched(csv_p) and not untouched(pq_p)


def test_replace_true_same_content_not_rewritten(written):
    d, csv_p, pq_p = written
    pin(csv_p, pq_p)
    cio.write_processed(df_of(3.0), d, "t", replace=True)
    assert untouched(csv_p) and untouched(pq_p)


def test_csv_written_by_old_code_is_byte_identical(tmp_path):
    # 旧コード（merged.to_csv(path)）が書いた CSV と、新コードの to_csv() の bytes が一致すること。
    # 一致しないと、マージ後の最初の夜に全系列を書き直してしまう。
    f = df_of(3.0)[cio.SCHEMA_COLUMNS].copy()
    f["date"] = pd.to_datetime(f["date"]).dt.strftime("%Y-%m-%d")
    f = f.drop_duplicates(subset=["date", "indicator_id", "region"], keep="last")
    f = f.sort_values(["date", "indicator_id", "region"]).reset_index(drop=True)
    f.to_csv(tmp_path / "t.csv", index=False, encoding="utf-8")
    f.to_parquet(tmp_path / "t.parquet", index=False)
    pin(tmp_path / "t.csv", tmp_path / "t.parquet")
    cio.write_processed(df_of(3.0), tmp_path, "t")
    assert untouched(tmp_path / "t.csv") and untouched(tmp_path / "t.parquet")
