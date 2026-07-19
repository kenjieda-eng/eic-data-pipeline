#!/usr/bin/env python3
"""朝刊サマリー JSON 生成（/today 復活 A 案・pipeline 側）。

data/catalog/indicators.json の csv_path から対象 6 行分の系列を読み込み、
data/today/{latest.json, archive/{date}.json, index.json} を決定的に生成する。

- 標準ライブラリのみ（csv / json / argparse / pathlib / datetime / zoneinfo / statistics）。
- 系列単位で try/except。読めない系列はスキップして警告（stderr）、残りで生成。
- lines が 0 件のときのみ exit 1。同日再実行は同内容で上書き（冪等）。
- --dry-run 時は latest.json 相当を stdout に出すのみ（ファイル書き込みなし）。
"""
import argparse
import csv
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "data" / "catalog" / "indicators.json"
TODAY_DIR = ROOT / "data" / "today"
JST = ZoneInfo("Asia/Tokyo")

# datetime.weekday(): 月=0 ... 日=6
WEEKDAY_JP = "月火水木金土日"

JEPX_REGIONS = [
    "hokkaido", "tohoku", "tokyo", "chubu", "hokuriku",
    "kansai", "chugoku", "shikoku", "kyushu",
]
REGION_JP = {
    "hokkaido": "北海道", "tohoku": "東北", "tokyo": "東京", "chubu": "中部",
    "hokuriku": "北陸", "kansai": "関西", "chugoku": "中国", "shikoku": "四国",
    "kyushu": "九州",
}

RELATED_INSIGHTS = [
    "jp-power-markets-three-layers",
    "electricity-bill-structure",
    "fuel-cost-adjustment",
]

# 対象 6 行（この順で出力）。
ROWS = [
    {"kind": "jepx-avg", "indicatorId": "derived:jepx-9-region-avg",
     "label": "JEPX 全国平均", "freq": "daily", "editor": "haru", "unit": "¥/kWh"},
    {"kind": "series", "indicatorId": "jepx-spot-tokyo",
     "label": "JEPX 東京", "freq": "daily", "editor": "haru"},
    {"kind": "series", "indicatorId": "jepx-spot-kyushu",
     "label": "JEPX 九州", "freq": "daily", "editor": "haru"},
    {"kind": "series", "indicatorId": "fuel-lng-jp-cif",
     "label": "LNG 日本着 (CIF)", "freq": "monthly", "editor": "haru"},
    {"kind": "series", "indicatorId": "fx-usdjpy-monthly-avg",
     "label": "USD/JPY (月中平均)", "freq": "monthly", "editor": "makoto"},
    {"kind": "series", "indicatorId": "jgb-10y-yield",
     "label": "JGB 10年利回り", "freq": "daily", "editor": "makoto"},
]


def load_series(csv_path):
    """CSV（date,indicator_id,region,value,source_url）を読み、
    value 欠損行をスキップして (date, value) の昇順リストを返す。"""
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            raw = (r.get("value") or "").strip()
            if raw == "":
                continue
            try:
                val = float(raw)
            except ValueError:
                continue
            rows.append((r["date"], val))
    rows.sort(key=lambda x: x[0])
    return rows


def position_word(range_pos_pct):
    if range_pos_pct >= 80:
        return "上位圏"
    if range_pos_pct <= 20:
        return "下位圏"
    return "中位圏"


def compute_fields(observations, freq, unit, label):
    """系列の観測列から数値フィールドとルールベース explanation を組み立てる。"""
    if len(observations) < 2:
        raise ValueError("観測が 2 件未満のため前期比を計算できない")

    data_date, value = observations[-1]
    prev_date, prev_value = observations[-2]

    diff = value - prev_value
    diff_pct = round(diff / abs(prev_value) * 100, 1) if prev_value != 0 else None

    window = 30 if freq == "daily" else 12
    recent_vals = [v for _, v in observations[-window:]]
    mn, mx = min(recent_vals), max(recent_vals)
    range_pos_pct = 50.0 if mx == mn else round((value - mn) / (mx - mn) * 100, 2)
    pos = position_word(range_pos_pct)

    pct_str = f"{diff_pct:+.1f}" if diff_pct is not None else "—"
    if freq == "daily":
        period_label = "前日比"
        explanation = (
            f"{data_date} の{label}は {value:.2f}{unit}"
            f"（前日比 {diff:+.2f}{unit}・{pct_str}%）。"
            f"直近30日のレンジでは{pos}にある。"
        )
    else:
        period_label = "前月比"
        explanation = (
            f"最新月（{data_date[:7]}）の{label}は {value:.2f}{unit}"
            f"（前月比 {pct_str}%）。"
            f"直近12ヶ月のレンジでは{pos}にある。"
        )

    return {
        "dataDate": data_date,
        "value": round(value, 2),
        "prevDate": prev_date,
        "prevValue": round(prev_value, 2),
        "diff": round(diff, 2),
        "diffPct": diff_pct,
        "periodLabel": period_label,
        "rangePosPct": range_pos_pct,
        "explanation": explanation,
    }


def compute_jepx_avg(by_id):
    """JEPX 9 エリアの単純平均系列（全 9 系列に値がある日のみ）を構築し、
    (観測列, 最新日の最高/最低エリア文) を返す。"""
    maps = {}
    for region in JEPX_REGIONS:
        ind = by_id[f"jepx-spot-{region}"]
        maps[region] = dict(load_series(ROOT / ind["csv_path"]))

    common = set(maps[JEPX_REGIONS[0]])
    for region in JEPX_REGIONS[1:]:
        common &= set(maps[region])
    if not common:
        raise ValueError("9 エリア共通の観測日が存在しない")

    common_dates = sorted(common)
    observations = [
        (d, statistics.mean(maps[r][d] for r in JEPX_REGIONS)) for d in common_dates
    ]

    data_date = common_dates[-1]
    day_vals = {r: maps[r][data_date] for r in JEPX_REGIONS}
    max_r = max(JEPX_REGIONS, key=lambda r: day_vals[r])
    min_r = min(JEPX_REGIONS, key=lambda r: day_vals[r])
    extra = (
        f"9エリアの最高は{REGION_JP[max_r]} {day_vals[max_r]:.2f}、"
        f"最低は{REGION_JP[min_r]} {day_vals[min_r]:.2f}。"
    )
    return observations, extra


def build_line(spec, by_id):
    if spec["kind"] == "jepx-avg":
        observations, extra = compute_jepx_avg(by_id)
        unit = spec["unit"]
        fields = compute_fields(observations, spec["freq"], unit, spec["label"])
        fields["explanation"] += extra
    else:
        ind = by_id[spec["indicatorId"]]
        unit = ind["unit"]
        observations = load_series(ROOT / ind["csv_path"])
        fields = compute_fields(observations, spec["freq"], unit, spec["label"])

    return {
        "indicatorId": spec["indicatorId"],
        "label": spec["label"],
        "unit": unit,
        "dataDate": fields["dataDate"],
        "value": fields["value"],
        "prevDate": fields["prevDate"],
        "prevValue": fields["prevValue"],
        "diff": fields["diff"],
        "diffPct": fields["diffPct"],
        "periodLabel": fields["periodLabel"],
        "rangePosPct": fields["rangePosPct"],
        "editor": spec["editor"],
        "explanation": fields["explanation"],
    }


def build_alerts(lines):
    alerts = []
    for line in lines:
        dp = line["diffPct"]
        if dp is None or abs(dp) < 3.0:
            continue
        alerts.append({
            "indicatorId": line["indicatorId"],
            "label": line["label"],
            "diffPct": dp,
            "message": f"{line['label']} が{line['periodLabel']} {dp:+.1f}% の変動",
        })
    return alerts


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="ファイルを書かず latest.json 相当を stdout に出力")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    catalog = json.load(open(CATALOG_PATH, encoding="utf-8"))
    by_id = {i["id"]: i for i in catalog["indicators"]}

    lines = []
    for spec in ROWS:
        try:
            lines.append(build_line(spec, by_id))
        except Exception as exc:  # noqa: BLE001 — 系列単位で握りつぶし残りを生成
            print(f"WARN: {spec['indicatorId']} をスキップ: {exc}", file=sys.stderr)

    if not lines:
        print("ERROR: 生成できた lines が 0 件", file=sys.stderr)
        return 1

    now = datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")
    latest = {
        "schema": "today-v1",
        "date": date_str,
        "weekday": WEEKDAY_JP[now.weekday()],
        "weekend": now.weekday() >= 5,
        "generatedAt": now.replace(microsecond=0).isoformat(),
        "lines": lines,
        "alerts": build_alerts(lines),
        "relatedInsights": RELATED_INSIGHTS,
    }

    if args.dry_run:
        print(json.dumps(latest, ensure_ascii=False, indent=2))
        return 0

    write_json(TODAY_DIR / "latest.json", latest)
    write_json(TODAY_DIR / "archive" / f"{date_str}.json", latest)

    index_path = TODAY_DIR / "index.json"
    dates = set()
    if index_path.exists():
        try:
            dates = set(json.load(open(index_path, encoding="utf-8")).get("dates", []))
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: 既存 index.json を無視: {exc}", file=sys.stderr)
    dates.add(date_str)
    write_json(index_path, {"schema": "today-index-v1", "dates": sorted(dates)})

    print(
        f"wrote data/today/latest.json + archive/{date_str}.json "
        f"({len(lines)} lines, {len(latest['alerts'])} alerts)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
