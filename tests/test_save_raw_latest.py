"""#67（2026-10-15）の save_raw: 名前に取得日（_YYYYMMDD）がある raw は、同じ名前の「いちばん新しい前の raw」と
中身が同じなら書かない。比べる相手が「いちばん新しい前の raw」であることを確かめる（2026-11-09、#75 のレビュー）。
比べる相手を「いちばん古い raw」にしてしまうと、値が A → B → A と戻った日の A が書かれず、raw 台帳は B のまま
になる。ネットに出ない・data/ に触らない（一時フォルダだけ）。"""
from __future__ import annotations

from pathlib import Path

import scripts.common.io as io


def _names(d: Path) -> list:
    return sorted(p.name for p in d.iterdir())


def test_value_reverts_is_written(tmp_path):
    # A → B → A: 3 日目の A は、いちばん新しい前の raw（B）と違うので書く
    io.save_raw(b"date,value\n2026-10-01,1.0\n", tmp_path, "x_20261101.csv")
    io.save_raw(b"date,value\n2026-10-01,2.0\n", tmp_path, "x_20261102.csv")
    io.save_raw(b"date,value\n2026-10-01,1.0\n", tmp_path, "x_20261103.csv")
    assert _names(tmp_path) == ["x_20261101.csv", "x_20261102.csv", "x_20261103.csv"]


def test_same_as_latest_not_written(tmp_path):
    # A → B → B: 3 日目の B は、いちばん新しい前の raw（B）と同じなので書かない
    io.save_raw(b"date,value\n2026-10-01,1.0\n", tmp_path, "x_20261101.csv")
    io.save_raw(b"date,value\n2026-10-01,2.0\n", tmp_path, "x_20261102.csv")
    io.save_raw(b"date,value\n2026-10-01,2.0\n", tmp_path, "x_20261103.csv")
    assert _names(tmp_path) == ["x_20261101.csv", "x_20261102.csv"]


def test_other_name_is_not_compared(tmp_path):
    # 名前（日付を除いた部分）が違う raw とは比べない
    io.save_raw(b"date,value\n2026-10-01,1.0\n", tmp_path, "y_20261101.csv")
    io.save_raw(b"date,value\n2026-10-01,1.0\n", tmp_path, "x_20261102.csv")
    assert _names(tmp_path) == ["x_20261102.csv", "y_20261101.csv"]
