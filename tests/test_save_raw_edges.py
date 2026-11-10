"""#67 の save_raw で、テストが無かった 3 つの道（2026-11-10、#76 のレビューの残り）:
同じ日の 2 回目の保存は上書き・比べる相手は「新しいファイルの日付より前」の raw だけ・拡張子も名前の一部。
ネットに出ない・data/ に触らない（一時フォルダだけ）。"""
from __future__ import annotations

from pathlib import Path

import scripts.common.io as io

A = b"date,value\n2026-10-01,1.0\n"
B = b"date,value\n2026-10-01,2.0\n"


def _names(d: Path) -> list:
    return sorted(p.name for p in d.iterdir())


def test_same_day_second_save_overwrites(tmp_path):
    # 11/1 に A、11/2 の 1 回目に B、同じ 11/2 の 2 回目に A（手動の再実行など）。
    # 同じ名前が既にあるときは前の日と比べずに上書きするので、11/2 のファイルは A になる
    io.save_raw(A, tmp_path, "x_20261101.csv")
    io.save_raw(B, tmp_path, "x_20261102.csv")
    io.save_raw(A, tmp_path, "x_20261102.csv")
    assert _names(tmp_path) == ["x_20261101.csv", "x_20261102.csv"]
    assert (tmp_path / "x_20261102.csv").read_bytes() == A


def test_later_dated_raw_is_not_compared(tmp_path):
    # 後の日付の raw（11/5）が先にあっても、11/3 のファイルは「11/3 より前」に同じ名前が無いので書く
    (tmp_path / "x_20261105.csv").write_bytes(A)
    io.save_raw(A, tmp_path, "x_20261103.csv")
    assert _names(tmp_path) == ["x_20261103.csv", "x_20261105.csv"]


def test_compares_with_latest_earlier_even_if_later_exists(tmp_path):
    # 11/1 が A、11/5 が B のとき、11/3 の A は「11/3 より前でいちばん新しい」11/1（A）と同じなので書かない
    (tmp_path / "x_20261101.csv").write_bytes(A)
    (tmp_path / "x_20261105.csv").write_bytes(B)
    io.save_raw(A, tmp_path, "x_20261103.csv")
    assert _names(tmp_path) == ["x_20261101.csv", "x_20261105.csv"]


def test_extension_is_part_of_the_name(tmp_path):
    # 拡張子が違えば別の名前（FRED は同じ系列に .json と .csv がある）。中身が同じでも書く
    io.save_raw(A, tmp_path, "x_20261101.json")
    io.save_raw(A, tmp_path, "x_20261102.csv")
    assert _names(tmp_path) == ["x_20261101.json", "x_20261102.csv"]
