"""2026-11-08（#75）: FRED API の応答（JSON）は先頭と全観測値に realtime_start / realtime_end が入り、応答ごとに変わる
（取得日ではなく、FRED が返した版の日付）。save_raw の「前回と同じ中身なら書かない」で、この値だけを除いて比べる
（保存する中身は受け取ったまま。値が 1 つでも変われば保存）。#72（e-Stat の RESULT.DATE）と同じ型。ネットに出ない・鍵はダミー。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import scripts.common.io as io
import scripts.fetch_us_macro as m

R = io.FRED_REALTIME_RE
SRC = {
    "api_base": "https://api.stlouisfed.org/fred/series/observations",
    "csv_base": "https://fred.stlouisfed.org/graph/fredgraph.csv",
}


class _Resp:
    def __init__(self, b: bytes):
        self.content = b
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return json.loads(self.content.decode("utf-8"))


@pytest.fixture
def day1(fixtures_dir: Path) -> bytes:
    # 2026-10-23 に取った UNRATE の API 応答（raw の UNRATE_20261023.json と同じ。realtime_start / realtime_end はすべて 2026-10-21）
    return (fixtures_dir / "fred" / "unrate_api.json").read_bytes()


def _shift(b: bytes, day: str) -> bytes:
    """realtime の日付だけを day に変えた「値が同じ別の応答」。"""
    out = re.sub(rb'("realtime_(?:start|end)":)"2026-10-21"',
                 lambda mm: mm.group(1) + b'"' + day.encode() + b'"', b)
    assert out != b
    return out


def _change_last_value(b: bytes) -> bytes:
    """最後の観測値を 1 つだけ変える（FRED が値を改定した日）。"""
    i = b.rindex(b'"value":"')
    j = b.index(b'"', i + len(b'"value":"'))
    return b[:i] + b'"value":"99.9' + b[j:]


def _api_run(monkeypatch, tmp_path: Path, content: bytes, tag: str) -> list:
    monkeypatch.setattr(m, "get", lambda url, params=None, timeout=60, **_kw: _Resp(content))
    monkeypatch.setenv("FRED_API_KEY", "dummy-key")
    df = m.fetch_series_raw(SRC, "UNRATE", tmp_path, tag)
    assert len(df) > 0
    return sorted(p.name for p in tmp_path.iterdir())


def test_fixture_premise(day1):
    day2 = _shift(day1, "2026-10-22")
    assert day1 != day2
    # 先頭の 2 つと、全観測値の 2 つずつにだけ当たる（value など、ほかの欄には当たらない）
    assert len(R.findall(day1)) == 2 + 2 * len(json.loads(day1)["observations"])
    assert R.sub(rb'\1"-"', day1) == R.sub(rb'\1"-"', day2)


def test_next_day_same_values_not_written(monkeypatch, tmp_path, day1):
    _api_run(monkeypatch, tmp_path, day1, "20261021")
    raws = _api_run(monkeypatch, tmp_path, _shift(day1, "2026-10-22"), "20261022")
    assert raws == ["UNRATE_20261021.json"]


def test_next_day_value_changed_written(monkeypatch, tmp_path, day1):
    _api_run(monkeypatch, tmp_path, day1, "20261021")
    changed = _change_last_value(_shift(day1, "2026-10-22"))
    raws = _api_run(monkeypatch, tmp_path, changed, "20261022")
    assert raws == ["UNRATE_20261021.json", "UNRATE_20261022.json"]
    assert (tmp_path / "UNRATE_20261022.json").read_bytes() == changed


def _set_last_value(b: bytes, v: str) -> bytes:
    i = b.rindex(b'"value":"')
    j = b.index(b'"', i + len(b'"value":"'))
    return b[:i] + b'"value":"' + v.encode() + b[j:]


def test_integer_value_change_written(monkeypatch, tmp_path, day1):
    # PAYEMS（雇用者数）のように値が整数の系列でも、値の改定は保存される（正規表現が value に広がっていないこと）
    a = _set_last_value(day1, "159044")
    b = _set_last_value(_shift(day1, "2026-10-22"), "159128")
    _api_run(monkeypatch, tmp_path, a, "20261021")
    raws = _api_run(monkeypatch, tmp_path, b, "20261022")
    assert raws == ["UNRATE_20261021.json", "UNRATE_20261022.json"]


def test_saved_content_is_as_received(monkeypatch, tmp_path, day1):
    _api_run(monkeypatch, tmp_path, day1, "20261021")
    assert (tmp_path / "UNRATE_20261021.json").read_bytes() == day1


def test_without_ignore_re_realtime_only_difference_is_written(tmp_path, day1):
    # ignore_re を渡さないソースは従来どおり（#67）: 1 バイトでも違えば書く
    (tmp_path / "x_20261021.json").write_bytes(day1)
    io.save_raw(_shift(day1, "2026-10-22"), tmp_path, "x_20261022.json")
    assert (tmp_path / "x_20261022.json").exists()
