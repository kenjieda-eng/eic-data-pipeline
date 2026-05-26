# Phase 2 国際ドメイン 第1バッチ スコーピング (2026-05-26)

## ゴール

新ドメイン `international` を立ち上げ、20 系列を月次粒度で取得する。
Phase 2 全体の最初の批量 (Batch 1)。

## 系列マニフェスト (20 系列)

### A. ECB SDMX API (5 系列) — `scripts/fetch_ecb.py` 新規

| indicator_id | Dataflow / Key | 単位 | 集約 | カバレッジ |
| --- | --- | --- | --- | --- |
| `ecb-rate-dfr` | `FM/D.U2.EUR.4F.KR.DFR.LEV` | % | monthly_end (daily→ffill→月末) | 1999-01〜 |
| `ecb-rate-mrr` | `FM/D.U2.EUR.4F.KR.MRR_FR.LEV` | % | monthly_end | 1999-01〜 |
| `ecb-rate-mlf` | `FM/D.U2.EUR.4F.KR.MLFR.LEV` | % | monthly_end | 1999-01〜 |
| `fx-eurusd-monthly-avg` | `EXR/M.USD.EUR.SP00.A` | USD/EUR | monthly_mean | 1999-01〜 |
| `fx-eurjpy-monthly-avg` | `EXR/M.JPY.EUR.SP00.A` | JPY/EUR | monthly_mean | 1999-01〜 |

- License: `ecb-terms` (出典明示で再利用可)
- Base URL: `https://data-api.ecb.europa.eu/service/data`
- 政策金利は **政策金利変更日のみ tick** の日次データ → 日次 ffill → 月末値抽出で月次化。
  ECB 側に月次集約版 (`FM/M.*.LEV`) は存在しない (実 API で 404 を確認)。

### B. Ember Monthly Electricity Data (15 系列) — `scripts/fetch_ember.py` 新規

5 ヶ国 × 3 指標:

| 国 (region) | demand | generation | co2-intensity |
| --- | --- | --- | --- |
| 日本 (jp) | `ember-demand-jp` | `ember-generation-jp` | `ember-co2-intensity-jp` |
| 米国 (us) | `ember-demand-us` | `ember-generation-us` | `ember-co2-intensity-us` |
| 中国 (cn) | `ember-demand-cn` | `ember-generation-cn` | `ember-co2-intensity-cn` |
| ドイツ (de) | `ember-demand-de` | `ember-generation-de` | `ember-co2-intensity-de` |
| 英国 (gb) | `ember-demand-gb` | `ember-generation-gb` | `ember-co2-intensity-gb` |

- demand / generation: TWh, monthly_sum
- co2-intensity: gCO2/kWh, monthly_mean
- License: `CC-BY-4.0` (Ember, 商用・再配布可)
- 安定 URL: `https://files.ember-energy.org/public-downloads/monthly_full_release_long_format.csv`
- カバレッジ: 多くの国は 2015-01〜、米国は 2001-01〜、日本の co2-intensity は 2018-04〜

## Phase 0 検証ログ (L-063)

| 仕様書記載 | 実 API 検証結果 | 採用判断 |
| --- | --- | --- |
| ECB DFR/MRR/MLF | `FM/D.U2.EUR.4F.KR.{DFR,MRR_FR,MLFR}.LEV` 200 OK、変化日 tick の daily | 採用、月末値で月次化 |
| ECB EUR/USD | `EXR/M.USD.EUR.SP00.A` 200 OK、monthly avg 直接取得 | 採用 |
| BOJ FM08 EUR/JPY | **FM08 に EUR/JPY 系列なし** (`FXERM01-29` を全数スキャン、USD/JPY 10 系列のみ) | **取下げ → ECB EXR `M.JPY.EUR.SP00.A` に切替** |
| Ember 月次 CSV | `files.ember-energy.org/public-downloads/monthly_full_release_long_format.csv` 200 OK、約 70 MB long-format | 採用 |

## 主要変更点

- `scripts/common/metadata.py`: `LICENSE_VALUES` に `ecb-terms` を追加。
- `docs/source_map.yaml`: `ecb` / `ember` セクションを末尾に追加 (`boj-fx` は無変更)。
- `scripts/fetch_ecb.py`: 新規。SDMX REST API + 月末値集約。
- `scripts/fetch_ember.py`: 新規。long-format CSV を 1 リクエストで取得、抽出。
- `.github/workflows/nightly-fetch.yml`: ECB / Ember の 2 ステップを `Generate indicators catalog` の直前に追加。

## 値の妥当性 (smoke test 2026-05-26 実取得)

- ECB DFR 2026-05: **2.00 %** (2026 春の利下げ後水準) ✅
- EUR/USD 2026-04: **1.1706** ✅
- EUR/JPY 2026-04: **186.21** (円安進行で実勢、想定 150-170 より高め) ✅
- 日本月間発電量 2026-02: **76.31 TWh** (冬季水準として妥当) ✅
- 日本 CO2 強度 2025-12: **476.46 gCO2/kWh** (火力比率高い日本で妥当) ✅

## DoD 達成状況

- [x] 各 fetch が通常モードで実取得 (合成データなし、L-063 準拠)
- [x] `--backfill` 引数を実装 (ECB: 1999-01 から、Ember: 常に全期間)
- [x] `generate_catalog.py` 実行で errors 0、新 20 系列が `indicators.json` (version 2 / schema D-017) に `csv_path` 付きで格納
- [x] Required 11 フィールド充足
- [x] ライセンス識別子正確 (`ecb-terms` / `CC-BY-4.0`)、`source_url` 明記
- [x] PR ベース (D-014)

## 既知の鮮度警告 (CI block しない)

- Ember co2-intensity 5 系列: 排出強度の確定は他指標より 2-3 ヶ月遅れるため SLA 90 日に対し 176 日。Phase 1 後半で indicator 別 SLA を 180 へ緩和することを検討。
- Ember demand/generation-jp: 日本のみ Ember 側で 1 ヶ月余分に遅れる傾向。SLA 90 → 120 でも良いが Phase 1 観察後に判断。
- fx-eurusd / fx-eurjpy: 月平均は翌月初に確定するため、SLA 45 日に対し 55 日。boj-fx の 60 日 SLA に揃える調整は Phase 1 後半で検討。

## 次バッチ候補 (Phase 2 Batch 2 以降)

- 他通貨 (EUR/CNY, EUR/GBP) を ECB EXR から追加
- IEA / EIA 月次エネルギー指標 (発電燃料消費、石油生産)
- OECD Composite Leading Indicator
- 国際海運運賃 (BDI, BSI など)

## 参照

- 規律: L-013 / L-058 / L-062 / L-063 / D-009 / D-017 (`docs/decisions.md`)
- 一次ソース API
  - ECB SDMX API: https://data.ecb.europa.eu/help/api/overview
  - Ember Monthly Electricity: https://ember-energy.org/data/monthly-electricity-data/
