"""fred-macro を raw 台帳の凍結の見張りに加えた宣言のテスト（2026-11-12、リン）。

#75（11/8）で FRED API の raw から realtime_start/end の揺れを除いたので、fred-macro の raw は
上流が本当に新しい値を出した日だけ増える。7 系列はすべて月次（CPI 3 本・雇用 2 本・FF 金利・鉱工業生産）で、
公表日がずれているため、ふだんは 2 週間あかずにどれかが変わる。30 日を超えて 1 バイトも変わらなければ、
キー切れ・API の仕様変更・取得の停止を疑う（report-only。D-020⑤-1 と同じ扱い）。
ネットに出ない・鍵を読まない・data/ に触らない（一時フォルダだけ）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from scripts import raw_ledger


def _watch(repo_root):
    return raw_ledger.load_watch(repo_root / "docs" / "source_map.yaml")


def test_fred_macro_is_declared(repo_root):
    w = _watch(repo_root)
    assert w["fred-macro"] == {"source": "fred-macro", "freeze_days": 30, "notify_change": False}


def test_existing_watches_unchanged(repo_root):
    w = _watch(repo_root)
    assert sorted(w) == ["eia_intl", "ember", "enecho-power", "fit", "fred-macro", "fuel", "gio"]


def _raw(tmp_path, body: str):
    d = tmp_path / "raw" / "fred-macro"
    d.mkdir(parents=True, exist_ok=True)
    (d / "CPIAUCSL_20261111.json").write_text(body, encoding="utf-8")
    return tmp_path / "raw"


def test_freeze_after_30_days(tmp_path, repo_root):
    w = {"fred-macro": _watch(repo_root)["fred-macro"]}
    raw = _raw(tmp_path, json.dumps({"observations": [{"date": "2026-10-01", "value": "337.476"}]}))
    ledger = {"schema": raw_ledger.SCHEMA, "updated_at": None, "sources": {}, "assessment": None}
    day0 = date(2026, 11, 11)
    raw_ledger.update_ledger(ledger, raw, day0, w)

    r30 = raw_ledger.update_ledger(ledger, raw, day0 + timedelta(days=30), w)
    a30 = raw_ledger.assess(r30, w, day0 + timedelta(days=30))
    assert a30["frozen"] == []

    r31 = raw_ledger.update_ledger(ledger, raw, day0 + timedelta(days=31), w)
    a31 = raw_ledger.assess(r31, w, day0 + timedelta(days=31))
    assert [f["dir"] for f in a31["frozen"]] == ["fred-macro"]
    assert a31["frozen"][0]["days"] == 31


def test_change_resets_and_is_not_notified(tmp_path, repo_root):
    w = {"fred-macro": _watch(repo_root)["fred-macro"]}
    raw = _raw(tmp_path, '{"v": 1}')
    ledger = {"schema": raw_ledger.SCHEMA, "updated_at": None, "sources": {}, "assessment": None}
    day0 = date(2026, 11, 11)
    raw_ledger.update_ledger(ledger, raw, day0, w)
    (raw / "fred-macro" / "PAYEMS_20261204.json").write_text('{"v": 2}', encoding="utf-8")
    day = day0 + timedelta(days=23)
    r = raw_ledger.update_ledger(ledger, raw, day, w)
    a = raw_ledger.assess(r, w, day)
    assert r["fred-macro"]["changed"] is True
    assert a["frozen"] == []
    assert a["changed"] == []  # notify_change: false（改定は改訂台帳が processed で見る）
