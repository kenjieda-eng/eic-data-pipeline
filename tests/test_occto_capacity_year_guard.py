"""#77 のレビュー（2026-11-10）: リンクの文言から年度を拾う保険で、別の年度の数字を新しい年度として出さないための守り。
- 年度を含まないファイル名は、raw の置き場（キャッシュ）で年度どうしがぶつからないよう、年度を前に付けた名前で保存する
- ZIP の中の CSV の年度が、処理している年度と違えば FiscalYearMismatch で run を失敗にする（これまでは WARNING だけ）
ネットに出ない・data/ に触らない（置き場は一時フォルダに差し替える）。"""
from __future__ import annotations

from pathlib import Path

import pytest

import scripts.fetch_occto_capacity as m

BASE = "https://www.occto.or.jp/capacity-market/yoryoshijyo/main/data/"


def _page(*links: tuple) -> str:
    body = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
    return f"<html><body>{body}</body></html>"


def _text(fy: int) -> str:
    return f"容量市場メインオークション約定結果（対象実需給年度：{fy}年度）(1KB)"


def test_yearless_filenames_get_distinct_cache_names():
    html = _page(("/x/2030/yakujoukekka_csv.zip", _text(2030)), ("/x/2031/yakujoukekka_csv.zip", _text(2031)))
    zips = m.list_auction_zips(html, base_url=BASE)
    assert [(z["delivery_fy"], z["filename"]) for z in zips] == [
        (2030, "2030_yakujoukekka_csv.zip"),
        (2031, "2031_yakujoukekka_csv.zip"),
    ]
    assert zips[0]["url"].endswith("/x/2030/yakujoukekka_csv.zip")


def test_parse_with_wrong_fiscal_year_raises(fixtures_dir: Path):
    zip_bytes = (fixtures_dir / "occto_capacity" / "2029_yakujoukekka_csv.zip").read_bytes()
    with pytest.raises(m.FiscalYearMismatch):
        m.parse_auction_zip_csv(zip_bytes, m.load_source_cfg(), 2030)


def test_mismatch_fails_the_run(monkeypatch, tmp_path, fixtures_dir: Path):
    # FY2030 のリンクから FY2029 の中身が返った（キャッシュの取り違えなど）→ 書く前に例外で止まる
    zip_bytes = (fixtures_dir / "occto_capacity" / "2029_yakujoukekka_csv.zip").read_bytes()
    monkeypatch.setattr(m, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(m, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(m, "fetch_index_page", lambda url: _page(("/x/2030/yakujoukekka_csv.zip", _text(2030))))
    monkeypatch.setattr(m, "download_auction_zip", lambda url, dest: zip_bytes)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    cfg = dict(m.load_source_cfg())
    with pytest.raises(m.FiscalYearMismatch):
        m.fetch_all_auctions(cfg, backfill=False, dry_run=False)
    assert not (tmp_path / "processed").exists() or not any((tmp_path / "processed").iterdir())
