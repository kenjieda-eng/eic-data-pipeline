# 共通スキーマ定義

> `data/processed/` 配下のすべての CSV / Parquet ファイルは、以下のスキーマに揃える。

---

## long 形式 5 列

| 列名 | 型 | NULL | 説明 |
|---|---|---|---|
| `date` | `YYYY-MM-DD` 文字列 | 不可 | 観測日または受渡日（JST） |
| `indicator_id` | 文字列 | 不可 | モック (energy-data-platform) と共通の指標 ID |
| `region` | 文字列 | 可 | 地域コード。全国なら `jp`、エリア別は `hokkaido`/`tohoku`/`tokyo`/`chubu`/`hokuriku`/`kansai`/`chugoku`/`shikoku`/`kyushu` |
| `value` | 数値 (float64) | 可 | 観測値。単位は `docs/source_map.yaml` の当該指標で定義 |
| `source_url` | URL 文字列 | 不可 | その行の値が由来する一次ソース URL |

---

## ユニーク制約

`(date, indicator_id, region)` で一意。
同じ組み合わせで値が更新された場合は **last-write-wins**（`scripts/common/io.py` の `write_processed` が担当）。

---

## 地域コード

モック側の命名と合わせる:

```
jp        全国
hokkaido  北海道
tohoku    東北
tokyo     東京
chubu     中部
hokuriku  北陸
kansai    関西
chugoku   中国
shikoku   四国
kyushu    九州
```

国際指標の場合は ISO 3166-1 alpha-2（`us`, `de` 等）。

---

## ファイル配置

```
data/processed/{source}/{basename}.csv
data/processed/{source}/{basename}.parquet
```

- `source` は `source_map.yaml` のトップレベルキー（例: `jepx`）
- `basename` は任意だが、通常は `{indicator_id}` または `{source}_all`

例:
- `data/processed/jepx/jepx-spot-system.csv`
- `data/processed/jepx/jepx-spot-tokyo.parquet`

---

## 欠損・訂正の扱い

- 取得できなかった日は行を作らない（空行で埋めない）
- 一次ソース側で訂正が入った場合は、次回取得時に last-write-wins で上書きされる
- 「この日のデータを差し替えた」という履歴は Git の履歴（コミット）で追える

---

## raw ファイルとの関係

`data/raw/{source}/` には一次ソースから取得したオリジナルファイル（.csv, .xlsx, .json 等）を **無加工で** 保存する。
`data/processed/` はそれを long 形式に正規化したもの。

両方を保存する理由:
- raw は **再現性**（後から別の抽出ロジックで再処理できる）
- processed は **使いやすさ**（研究者・可視化がすぐ使える形）

---

*最終更新: 2026-04-21*
