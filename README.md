# eic-data-pipeline

> 日本のエネルギーと金融の公開データを、毎日自動で収集・整形・公開するパイプライン。
> EIC Data プロジェクト（一般社団法人エネルギー情報センター運営）の基盤インフラ。

---

## これは何

モック（[energy-data-platform](https://github.com/kenjieda-eng/energy-data-platform)）で定義した **167 指標** に対して、
一次ソース（JEPX / OCCTO / 気象庁 / 経産省 / 日銀 / 環境省 等）から **毎朝 JST 8:00 に自動取得** し、
このリポジトリの `data/` に CSV / Parquet 形式でコミットします。

ダウンロードしたデータは **CC BY 4.0** で再配布自由。研究者・ジャーナリスト・政策担当者・市民が直接利用できます。

### なぜこの形なのか

- **公開 API やスクレイピングで取れる無料データだけを扱う**（[D-002](./docs/decisions.md) の方針）
- **鮮度より「無料・取得可能」を優先**（[D-009](./docs/decisions.md)、前日データで十分）
- **GitHub Actions で nightly 実行 → 自分たちのリポジトリにコミット**（[D-010](./docs/decisions.md)）
- **Git の履歴そのものが訂正ログの原材料になる**

---

## リポジトリ構成

```
eic-data-pipeline/
├── README.md                       この文書
├── LICENSE                         コードのライセンス（MIT）
├── DATA_LICENSE.md                 データのライセンス（CC BY 4.0）
├── .gitignore
│
├── .github/
│   └── workflows/
│       └── nightly-fetch.yml       毎朝 JST 8:00 に全スクリプトを順次実行
│
├── scripts/
│   ├── common/
│   │   ├── http.py                 共通 HTTP ヘルパ（UA、リトライ、429/5xx）
│   │   └── io.py                   CSV / Parquet 書き出しヘルパ
│   └── fetch_jepx.py               JEPX スポット価格（最初の実装）
│
├── data/
│   ├── raw/jepx/                   生ファイル（一次ソースから落ちてきたそのまま）
│   └── processed/jepx/             共通スキーマに整形した CSV / Parquet
│
└── docs/
    ├── source_map.yaml             指標 ID → ソース URL の対応表（機械可読）
    └── schema.md                   共通スキーマ定義（列名・型・単位）
```

---

## 共通スキーマ（`data/processed/` 配下）

すべての処理済み CSV は以下の **5 列** に正規化されます。

| 列名 | 型 | 説明 |
|---|---|---|
| `date` | `YYYY-MM-DD` | 観測日（または受渡日） |
| `indicator_id` | 文字列 | モックと共通の ID（例: `jepx-spot`） |
| `region` | 文字列 (nullable) | 地域コード。全国なら `jp`、エリア別は `tokyo` 等 |
| `value` | 数値 (nullable) | 観測値。単位は `docs/source_map.yaml` で定義 |
| `source_url` | URL | その値の一次ソース |

詳細は [`docs/schema.md`](./docs/schema.md)。

---

## 実行方法

### 自動（通常の運用）

- GitHub Actions が毎朝 JST 8:00 に全スクリプトを実行します
- 新しいデータがあれば自動で commit & push
- 失敗時は GitHub から通知メール（登録アドレスへ）

### 手動（動作確認や過去分の取り込み）

```bash
# Python 3.11 想定
pip install -r requirements.txt

# JEPX を取得
python scripts/fetch_jepx.py

# 直近 3 年分を取得したい場合
python scripts/fetch_jepx.py --years 3
```

### GitHub Actions の手動トリガー

GitHub のリポジトリページ → `Actions` タブ → `Nightly Fetch` → `Run workflow` ボタン。

---

## ライセンス

- **コード**: [MIT](./LICENSE)
- **データ**: [CC BY 4.0](./DATA_LICENSE.md) — 出典明記で自由に再利用可能

### 一次ソースの表記

このリポジトリのデータは以下の一次ソースに基づきます（`docs/source_map.yaml` 参照）：

- JEPX（日本卸電力取引所）— https://www.jepx.jp/
- 電力広域的運営推進機関（OCCTO）— https://www.occto.or.jp/
- 気象庁（JMA）— https://www.data.jma.go.jp/
- 資源エネルギー庁（経産省）— https://www.enecho.meti.go.jp/
- 日本銀行 — https://www.boj.or.jp/
- 環境省 — https://www.env.go.jp/
- 他

---

## 貢献

当面はコア 11 名（エネルギー情報センター内）での運用です。外部からの PR・Issue 受付は β 公開後に再検討します（[D-004](./docs/decisions.md)）。

---

## 関連リポジトリ

- [energy-data-platform](https://github.com/kenjieda-eng/energy-data-platform) — モック・議論・全体設計（姉妹リポジトリ）

---

## 運営

一般社団法人エネルギー情報センター
姉妹サイト: [新電力ネット](https://pps-net.org/)
