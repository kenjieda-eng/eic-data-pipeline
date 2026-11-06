"""#72（2026-10-29）: e-Stat の応答は RESULT.DATE（応答を作った日時）が毎回変わる。save_raw の「前回と同じ中身なら
書かない」で、この値だけを除いて比べる（保存する中身は受け取ったまま。エラー応答は毎回保存）。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import scripts.common.io as io

R = io.ESTAT_RESULT_DATE_RE


@pytest.fixture
def days(fixtures_dir: Path):
    fx = fixtures_dir / "estat"
    # 2026-10-27 と 10-28 の jpn-industrial-production の応答（RESULT.DATE 以外は同じ。appId などの鍵は含まない）
    return (fx / "day1.json").read_bytes(), (fx / "day2.json").read_bytes()


def _written(tmp_path: Path, first: bytes, second: bytes, use_re: bool) -> bool:
    (tmp_path / "x_20261027.json").write_bytes(first)
    kw = {"ignore_re": R} if use_re else {}
    io.save_raw(second, tmp_path, "x_20261028.json", **kw)
    return (tmp_path / "x_20261028.json").exists()


def test_fixture_premise(days):
    d1, d2 = days
    assert d1 != d2
    assert R.sub(rb'\1"-"', d1) == R.sub(rb'\1"-"', d2)


def test_only_result_date_differs_with_ignore_re_not_written(tmp_path, days):
    d1, d2 = days
    assert _written(tmp_path, d1, d2, True) is False


def test_only_result_date_differs_without_ignore_re_written(tmp_path, days):
    d1, d2 = days
    assert _written(tmp_path, d1, d2, False) is True


def test_value_changed_written(tmp_path, days):
    d1, d2 = days
    changed = re.sub(rb'("\$":")(\d)',
                     lambda mm: mm.group(1) + (b"1" if mm.group(2) != b"1" else b"2"), d2, count=1)
    assert changed != d2
    assert _written(tmp_path, d1, changed, True) is True


def test_error_response_written(tmp_path, days):
    d1, d2 = days
    err = d2.replace(b'"STATUS":0,', b'"STATUS":100,', 1)
    assert err != d2
    assert _written(tmp_path, d1, err, True) is True


def test_error_responses_in_a_row_written(tmp_path, days):
    # エラー応答（STATUS が 0 以外）は RESULT.DATE を除かないので、日時が違えば続いても毎回保存される
    d1, d2 = days
    e1 = d1.replace(b'"STATUS":0,', b'"STATUS":100,', 1)
    e2 = d2.replace(b'"STATUS":0,', b'"STATUS":100,', 1)
    assert e1 != d1 and e2 != d2
    assert _written(tmp_path, e1, e2, True) is True


def test_identical_without_ignore_re_not_written(tmp_path, days):
    d1, _ = days
    assert _written(tmp_path, d1, d1, False) is False


@pytest.mark.parametrize("path", ["scripts/fetch_estat.py", "scripts/fetch_estat_trade.py"])
def test_estat_callers_use_ignore_re(repo_root, path):
    s = (repo_root / path).read_text(encoding="utf-8")
    calls = re.findall(r"save_raw\([^\n]*\)", s)
    assert calls, path
    assert all("ignore_re=ESTAT_RESULT_DATE_RE" in c for c in calls), calls
