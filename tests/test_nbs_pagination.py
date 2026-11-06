"""#70（2026-10-25）: NBS の Latest Releases は 1 ページ 15 件。PMI が 2 ページ目に押し出されても見つけ、
3 ページとも無ければ失敗（赤）にする。ネットに出ない（get を差し替える）。"""
from __future__ import annotations

from pathlib import Path

import pytest

import scripts.fetch_nbs_pmi as m

BASE = "https://www.stats.gov.cn/english/PressRelease/"
EXPECT = "https://www.stats.gov.cn/english/PressRelease/202610/t20261008_1965466.html"


class _Resp:
    def __init__(self, b: bytes):
        self.content = b
        self.apparent_encoding = "utf-8"
        self.status_code = 200

    def raise_for_status(self):
        return None


@pytest.fixture
def fx(fixtures_dir: Path) -> Path:
    return fixtures_dir / "nbs_pmi"


def _run(monkeypatch, fx: Path, tmp_path: Path, pages: dict):
    calls: list = []

    def fake_get(url, timeout=60, **_kw):
        calls.append(url)
        return _Resp((fx / pages[url]).read_bytes())

    monkeypatch.setattr(m, "get", fake_get)
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)
    return m.find_latest_pmi_url(BASE, tmp_path, "20261025"), calls


def test_found_on_page2(monkeypatch, fx, tmp_path):
    # 2026-10-24〜30 の実際の状態: 1 ページ目に無く、2 ページ目にある
    url, calls = _run(monkeypatch, fx, tmp_path, {
        BASE: "index_page1.html",
        BASE + "index_1.html": "index_page2.html",
        BASE + "index_2.html": "index_page3.html",
    })
    assert url == EXPECT
    assert len(calls) == 2


def test_found_on_page1(monkeypatch, fx, tmp_path):
    # 通常の状態（2 ページ目の写しを 1 ページ目として渡す）: 1 ページだけ取る
    url, calls = _run(monkeypatch, fx, tmp_path, {BASE: "index_page2.html"})
    assert url == EXPECT
    assert len(calls) == 1


def test_not_found_in_three_pages_raises(monkeypatch, fx, tmp_path):
    with pytest.raises(RuntimeError, match="pages 1-3"):
        _run(monkeypatch, fx, tmp_path, {
            BASE: "index_page1.html",
            BASE + "index_1.html": "index_page1.html",
            BASE + "index_2.html": "index_page1.html",
        })


def test_parse_release(fx):
    html = (fx / "release_t20261008_1965466.html").read_bytes().decode("utf-8", errors="replace")
    rows = m.parse_pmi_table(html)
    assert len(rows) == 13
    last = rows[-1] if isinstance(rows, list) else tuple(rows.iloc[-1].tolist())
    assert tuple(last)[0] == "2026-09-01"
    assert float(tuple(last)[1]) == 50.1
