"""容量市場（OCCTO）メインオークション約定結果の一覧ページから ZIP のリンクを拾う部分の守り（2026-11-10）。
- ファイル名の型（{fy}_yakujoukekka_csv.zip / {fy}_main_yakujoukekka_csv.zip）に合わない ZIP は、リンクの文言
  「…メインオークション約定結果（対象実需給年度：NNNN年度）」から年度を拾う
- 一覧が取れない・リンクが 1 本も拾えないときは例外（main が exit 1 を返し、年次の workflow が赤くなる）
ネットに出ない（保存した一覧ページを使う）・data/ に触らない（dry_run と、ZIP の読み取りだけ）。"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import scripts.fetch_occto_capacity as m

BASE = "https://www.occto.or.jp/capacity-market/yoryoshijyo/main/data/"


@pytest.fixture
def fx(fixtures_dir: Path) -> Path:
    return fixtures_dir / "occto_capacity"


@pytest.fixture
def index_html(fx: Path) -> str:
    # 2026-11-10 に取った一覧ページ（メインオークション FY2024〜FY2029 と、追加オークション FY2025〜FY2027 の ZIP がある）
    return (fx / "index_20261110.html").read_text(encoding="utf-8")


def _with_link(html: str, href: str, text: str) -> str:
    """一覧ページの末尾にリンクを 1 本足した HTML。"""
    assert html.count("</body>") == 1
    return html.replace("</body>", f'<a href="{href}">{text}</a></body>')


def _fys(zips: list) -> list:
    return [z["delivery_fy"] for z in zips]


def test_current_index_has_six_main_auctions(index_html):
    zips = m.list_auction_zips(index_html, base_url=BASE)
    assert _fys(zips) == [2024, 2025, 2026, 2027, 2028, 2029]
    by_fy = {z["delivery_fy"]: z for z in zips}
    assert by_fy[2028]["filename"] == "2028_main_yakujoukekka_csv.zip"  # FY2028 だけファイル名に _main_ が入る
    assert "/capacity-market/assets/" in by_fy[2029]["url"]             # FY2029 だけ置き場所が違う
    assert not any("tsuika" in z["filename"] for z in zips)             # 追加オークションは拾わない


def test_link_text_of_every_current_main_auction_gives_the_same_fy(index_html):
    # 文言からの判定（保険）が、今ある 6 本のどれでもファイル名からの判定と同じ年度になること
    texts = {a["href"].rsplit("/", 1)[-1]: a.get_text(" ", strip=True)
             for a in BeautifulSoup(index_html, "html.parser").find_all("a", href=True)}
    zips = m.list_auction_zips(index_html, base_url=BASE)
    assert len(zips) == 6
    for z in zips:
        mm = m.MAIN_AUCTION_LINK_TEXT_RE.search(texts[z["filename"]])
        assert mm is not None, texts[z["filename"]]
        assert int(mm.group(1)) == z["delivery_fy"]


def test_unknown_filename_falls_back_to_link_text(index_html):
    html = _with_link(index_html, "/capacity-market/assets/x/2030_mainauction_result.zip",
                      "容量市場メインオークション約定結果（対象実需給年度：2030年度）(1KB)")
    zips = m.list_auction_zips(html, base_url=BASE)
    assert _fys(zips) == [2024, 2025, 2026, 2027, 2028, 2029, 2030]
    assert zips[-1]["filename"] == "2030_mainauction_result.zip"
    assert zips[-1]["url"] == "https://www.occto.or.jp/capacity-market/assets/x/2030_mainauction_result.zip"


def test_known_filename_needs_no_link_text(index_html):
    html = _with_link(index_html, "/capacity-market/assets/x/2030_yakujoukekka_csv.zip", "ダウンロード")
    assert _fys(m.list_auction_zips(html, base_url=BASE))[-1] == 2030


def test_additional_auction_with_unknown_filename_is_not_picked(index_html):
    html = _with_link(index_html, "/capacity-market/assets/x/2030_additional_result.zip",
                      "容量市場追加オークション約定結果（対象実需給年度：2030年度）(1KB)")
    assert 2030 not in _fys(m.list_auction_zips(html, base_url=BASE))


def test_unrelated_zip_is_not_picked(index_html):
    html = _with_link(index_html, "/capacity-market/assets/x/2030_manual.zip", "容量市場システムマニュアル（2030年度）")
    assert 2030 not in _fys(m.list_auction_zips(html, base_url=BASE))


def test_no_links_raises(monkeypatch):
    monkeypatch.setattr(m, "fetch_index_page", lambda url: "<html><body>ただいまメンテナンス中です</body></html>")
    with pytest.raises(RuntimeError, match="1 本も"):
        m.fetch_all_auctions({"index_url": BASE}, backfill=False, dry_run=True)


def test_index_fetch_failure_raises(monkeypatch):
    def boom(url):
        raise ConnectionError("simulated")

    monkeypatch.setattr(m, "fetch_index_page", boom)
    with pytest.raises(ConnectionError):
        m.fetch_all_auctions({"index_url": BASE}, backfill=False, dry_run=True)


def test_dry_run_with_links_returns_zero(monkeypatch, index_html):
    monkeypatch.setattr(m, "fetch_index_page", lambda url: index_html)
    assert m.fetch_all_auctions({"index_url": BASE}, backfill=False, dry_run=True) == 0


def test_parse_fy2029_zip(fx):
    # data/raw/occto-capacity/2029_yakujoukekka_csv.zip の写し。9 エリア × {価格, 容量} + 全国 2 = 20 行
    zip_bytes = (fx / "2029_yakujoukekka_csv.zip").read_bytes()
    rows = m.parse_auction_zip_csv(zip_bytes, m.load_source_cfg(), 2029)
    assert len(rows) == 20
    assert {r["date"] for r in rows} == {"2029-04-01"}
    by_id = {r["indicator_id"]: r["value"] for r in rows}
    assert by_id["capacity-main-auction-price-tokyo"] == 15111.0
    # ZIP の中の CSV から、合計と加重平均を別に計算して突き合わせる
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        text = z.read(z.infolist()[0]).decode("cp932")
    recs = list(csv.reader(io.StringIO(text)))[1:]
    assert len(recs) == 9
    prices = [int(r[2]) for r in recs]
    vols = [int(r[3]) for r in recs]
    assert by_id["capacity-main-auction-volume-total"] == float(sum(vols)) == 166079863.0
    national = round(sum(p * v for p, v in zip(prices, vols)) / sum(vols), 2)
    assert by_id["capacity-main-auction-price-national"] == national == 13908.57
