# データのライセンス

このリポジトリの `data/` 配下に置かれる **データファイル（CSV、Parquet、JSON 等）** は、
[Creative Commons Attribution 4.0 International License (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/deed.ja) の下で公開されます。

**コード（`scripts/` など）は別途 [MIT License](./LICENSE) で提供されます。**

---

## 要するに

以下のことを **自由に** 行えます。

- **共有** — 複製、頒布、再配布
- **翻案** — 変更、改変、二次的著作物の作成
- **商用利用** — 商業目的での利用

**ただし以下の条件に従うこと。**

### 表示（Attribution）

以下の表記を付してください。

```
Source: EIC Data (https://github.com/kenjieda-eng/eic-data-pipeline) / 一般社団法人エネルギー情報センター
Licensed under CC BY 4.0
```

加えて、**各データの一次ソース**（JEPX、OCCTO、気象庁等）も併記してください。
指標ごとの一次ソースは [`docs/source_map.yaml`](./docs/source_map.yaml) で確認できます。

---

## 一次ソースのライセンスについて

このリポジトリは一次ソースから取得した公開データを **整形して再配布** するものです。
一次ソースによっては、独自の利用規約や商用利用制限が存在する場合があります。

一次ソースのライセンスと CC BY 4.0 が矛盾する場合、**一次ソースのライセンスが優先されます**。
各指標の一次ソース利用規約は `docs/source_map.yaml` の `license` フィールドに記録しています。

再配布に制限がある一次ソースのデータは **このリポジトリに含めない方針** です（[D-002](https://github.com/kenjieda-eng/energy-data-platform/blob/main/docs/decisions.md) に準拠）。

---

## 免責

- このデータは **研究・報道・教育** を主目的として提供されます
- 投資判断・法令遵守判断など **重要な意思決定の唯一の根拠として利用しないでください**
- 一次ソースの公表値と相違が生じた場合、**一次ソースが真** です。[`/corrections`](https://github.com/kenjieda-eng/energy-data-platform) で訂正履歴を公開します

---

## 正式な法的条文

CC BY 4.0 の正式な法的条文は以下にあります：

https://creativecommons.org/licenses/by/4.0/legalcode.ja

---

*この DATA_LICENSE.md は `data/` 配下のすべてのファイルに適用されます。*
*2026-04-21 策定 / 一般社団法人エネルギー情報センター*
