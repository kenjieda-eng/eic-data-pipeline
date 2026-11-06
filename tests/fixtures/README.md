# tests/fixtures — テスト用に保存した公開ページ・応答の写し（2026-11-06）

テストはネットに出ないため、取得元の応答をここに保存して使う。どれも公開されているページ・API の応答で、
**API の鍵（FRED_API_KEY）・e-Stat の appId は含まない**（保存時と 2026-11-06 に確認）。

| フォルダ | 中身 | 取得元 | 取得日 | 使うテスト |
|---|---|---|---|---|
| `nbs_pmi/` | Latest Releases の一覧 1〜3 ページ（`index_page1〜3.html`）と、2026 年 9 月分 PMI の発表（`release_t20261008_1965466.html`） | 中国国家統計局 英語版 https://www.stats.gov.cn/english/PressRelease/ | 2026-10-25 | `test_nbs_pagination.py`（#70） |
| `fred/` | UNRATE の API 応答（`unrate_api.json`）・CSV（`unrate_fredgraph.csv`）・2026-10-22 に返ったメンテナンス画面（`maintenance_page.html`） | FRED（セントルイス連邦準備銀行） https://fred.stlouisfed.org/ | 2026-10-22〜26 | `test_fred_reject_html.py`（#71） |
| `estat/` | 鉱工業生産指数の API 応答 2 日分（`day1.json` = 10/27・`day2.json` = 10/28。RESULT.DATE だけが違う） | e-Stat（政府統計の総合窓口） https://www.e-stat.go.jp/ | 2026-10-27・28 | `test_estat_ignore_result_date.py`（#72） |

取得元の様式が変わってテストが実態と合わなくなったら、新しい応答をここに保存し直し、取得日を更新する。
