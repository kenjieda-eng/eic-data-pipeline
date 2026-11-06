"""#73 の追補（2026-11-04）: parquet の書き出しに失敗したら古い（または途中で切れた）parquet を消す。
残すと「CSV が同じなら書かない」で、次に CSV が変わる夜まで古いままになる。無ければ次の夜に両方書く。"""
from __future__ import annotations

import os

import pandas as pd
import pytest

from scripts.common import io as cio

OLD_T = 1_000_000_000


def df_of(v3: float) -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2026-01-01", "2026-02-01", "2026-03-01"],
        "indicator_id": ["t"] * 3,
        "region": ["jp"] * 3,
        "value": [1.5, 2.25, v3],
        "source_url": ["https://example.invalid/x"] * 3,
    })


def _boom(self, *a, **k):
    raise RuntimeError("simulated parquet failure")


@pytest.fixture(autouse=True)
def _no_rewrite_env(monkeypatch):
    monkeypatch.delenv("EIC_REWRITE_PROCESSED", raising=False)


def test_failure_removes_old_parquet_then_next_night_rewrites(tmp_path, monkeypatch):
    csv_p, pq_p = cio.write_processed(df_of(3.0), tmp_path, "t")
    assert pq_p.exists()
    with monkeypatch.context() as mp:
        mp.setattr(pd.DataFrame, "to_parquet", _boom)
        cio.write_processed(df_of(4.0), tmp_path, "t")  # 例外は外に出ない（CSV だけ残す）
    assert b"4.0" in csv_p.read_bytes()
    assert not pq_p.exists()
    os.utime(csv_p, (OLD_T, OLD_T))
    cio.write_processed(df_of(4.0), tmp_path, "t")  # 次の夜（中身は同じ）→ parquet が無いので両方書く
    assert pq_p.exists() and int(csv_p.stat().st_mtime) != OLD_T
    assert float(pd.read_parquet(pq_p)["value"].iloc[2]) == 4.0


def test_failure_without_existing_parquet_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(pd.DataFrame, "to_parquet", _boom)
    csv_p, pq_p = cio.write_processed(df_of(5.0), tmp_path, "u")
    assert csv_p.exists() and not pq_p.exists()
