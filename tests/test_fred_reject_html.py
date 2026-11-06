"""#71（2026-10-26）: FRED がメンテナンス画面（HTML）を 200 で返しても raw に保存せず失敗にする（API・CSV の両経路）。
ネットに出ない。鍵はダミー（本物の FRED_API_KEY は読まない）。"""
from __future__ import annotations

import json

import pytest

import scripts.fetch_us_macro as m

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


# (id, 内容, API の応答, CSV の応答, 鍵, 期待 rows/error, 期待する raw)
CASES = [
    ("both-maintenance", "API も CSV もメンテナンス画面（10/22 の状態）",
     "maintenance_page.html", "maintenance_page.html", "dummy-key", "error", []),
    ("api-maintenance-csv-ok", "API がメンテナンス画面・CSV は正常",
     "maintenance_page.html", "unrate_fredgraph.csv", "dummy-key", "rows", ["UNRATE_20261026.csv"]),
    ("api-ok", "API が正常（通常の状態）",
     "unrate_api.json", "maintenance_page.html", "dummy-key", "rows", ["UNRATE_20261026.json"]),
    ("nokey-csv-ok", "鍵なし・CSV が正常",
     None, "unrate_fredgraph.csv", "", "rows", ["UNRATE_20261026.csv"]),
    ("nokey-csv-maintenance", "鍵なし・CSV がメンテナンス画面",
     None, "maintenance_page.html", "", "error", []),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_fetch_series_raw(monkeypatch, tmp_path, fixtures_dir, case):
    _id, _desc, api_fx, csv_fx, key, want_kind, want_raw = case
    fx = fixtures_dir / "fred"

    def fake_get(url, params=None, timeout=60, **_kw):
        name = api_fx if url.startswith(SRC["api_base"]) else csv_fx
        return _Resp((fx / name).read_bytes())

    monkeypatch.setattr(m, "get", fake_get)
    if key:
        monkeypatch.setenv("FRED_API_KEY", key)
    else:
        monkeypatch.delenv("FRED_API_KEY", raising=False)
    for name in (api_fx, csv_fx):
        if name:
            # fixture が無いと FileNotFoundError で「失敗した」になり、#71 の直しが無くても通ってしまう（#74 のレビュー）
            assert (fx / name).is_file(), name
    if want_kind == "error":
        # #71 の直し: HTML（メンテナンス画面）は ValueError で拒む。ほかの例外では通らない
        with pytest.raises(ValueError, match="HTML"):
            m.fetch_series_raw(SRC, "UNRATE", tmp_path, "20261026")
    else:
        df = m.fetch_series_raw(SRC, "UNRATE", tmp_path, "20261026")
        assert len(df) > 0
    assert sorted(p.name for p in tmp_path.iterdir()) == want_raw
