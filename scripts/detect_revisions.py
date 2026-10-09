#!/usr/bin/env python3
"""
scripts/detect_revisions.py — 既収載の値が動いた系列を記帳する（改訂台帳 data/ledger/revisions.json。D-020 §11、R-12 §4 / Y-12 §4 / R-13 §3）

nightly の fetch が終わった直後（raw 台帳の後・catalog 生成の前）に走り、作業ツリーの data/processed/**/*.csv を
コミット済み（HEAD）の同じファイルと突き合わせる。**既にあった日付の値が変わった点**（= 出典側の遡及改定、
または当方の処理変更）だけを「改訂」として記帳し、新しい日付の追加は改訂に数えない（added として件数だけ添える）。

■ 出力（data/ledger/revisions.json、新しい日が先頭）
    {"schema": 1, "generated_at": "...", "last_checked": "2026-10-08", "retention_days": 400, "max_dates_per_series": 500,
     "days": [
       {"date": "2026-10-07", "base": "39385a4", "head": "24fbaee",
        "summary": {"series_revised": 10, "points_revised": 1387, "max_rel_change": 0.0317},
        "series": [
          {"id": "ember-co2-intensity-gb", "source": "ember", "source_version": "sha256:...",
           "revised_count": 138, "added": 2, "filled": 0, "removed": 0, "dropped": 0,
           "range": ["2015-01-01", "2026-07-01"], "max_rel_change": 0.0317, "max_abs_change": 9.961,
           "worst": {"date": "2026-03-01", "old": 213.9, "new": 220.7},
           "revised_dates": ["2015-01-01", "..."], "truncated": false}]}]}
    - revised_count: 旧値も新値も非 null で値が違う点の数。filled: null → 値、removed: 値 → null、dropped: 日付ごと消えた点。
    - max_rel_change: |new−old| / |old|（old ≠ 0 の点だけ）。old = 0 しか無ければ null。max_abs_change は全点。
    - revised_dates: 改訂された日付の列挙（上限 max_dates_per_series。超えたら truncated: true。revised_count は真の件数）。
    - 改訂の無かった日は days に足さない（last_checked だけ進む）。retention_days より古い日は落とす。
    - source: docs/source_map.yaml の sources.<key>.indicator_ids から逆引き。source_version: data/ledger/raw-hash.json の
      sources.<key>.digest（raw 台帳に無い source は null）。
    bess-net 側の使い方（Y-12 §4）: max_rel_change 10% 超 → 引用値を洗う／1% 未満 → 記録のみ／1〜10% → 表示系列なら洗う。
    revised_dates で「表示中の断面が含まれるか」を機械判定する。

■ 使い方
    python scripts/detect_revisions.py                       # nightly: HEAD と作業ツリーを比較して記帳（既定）
    python scripts/detect_revisions.py --old <sha> --new <sha> --date YYYY-MM-DD   # 遡及（コミット間の比較）
    python scripts/detect_revisions.py --backfill-since 2026-09-30                 # 遡及一括: main の nightly コミットを日付順に隣り合わせて比較し、台帳を作り直す
    python scripts/detect_revisions.py --check-only          # 差分を表示するだけ（書かない）
    終了コードは常に 0（report-only。台帳側の不具合で nightly を止めない）。例外時は 1。
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import math
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = "data/processed"
OUT = ROOT / "data" / "ledger" / "revisions.json"
RAW_LEDGER = ROOT / "data" / "ledger" / "raw-hash.json"
SOURCE_MAP = ROOT / "docs" / "source_map.yaml"
JST = timezone(timedelta(hours=9))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("detect_revisions")


def git(args: list[str], repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def read_csv_text(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), dtype={"date": str, "indicator_id": str, "region": str}, float_precision="round_trip")
    if "region" not in df.columns:
        df["region"] = ""
    return df[["date", "region", "value"]]


def load_rev(repo: Path, rev: str, path: str) -> pd.DataFrame | None:
    try:
        return read_csv_text(git(["show", f"{rev}:{path}"], repo))
    except subprocess.CalledProcessError:
        return None


def load_worktree(repo: Path, path: str) -> pd.DataFrame | None:
    p = repo / path
    if not p.exists():
        return None
    return read_csv_text(p.read_text(encoding="utf-8"))


def changed_csvs(repo: Path, old: str, new: str | None) -> list[str]:
    """new=None は作業ツリー。M（変更）だけを対象にする（A/D は改訂ではない）。"""
    args = ["diff", "--name-status", old] + ([new] if new else []) + ["--", f"{PROCESSED}/*.csv", f"{PROCESSED}/**/*.csv"]
    out = git(args, repo)
    paths = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == "M" and parts[1].endswith(".csv"):
            paths.append(parts[1])
    return sorted(set(paths))


def is_null(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def compare(old: pd.DataFrame, new: pd.DataFrame, max_dates: int) -> dict | None:
    o = old.set_index(["date", "region"])["value"]
    n = new.set_index(["date", "region"])["value"]
    o = o[~o.index.duplicated(keep="last")]
    n = n[~n.index.duplicated(keep="last")]
    common = o.index.intersection(n.index)
    revised_dates: list[str] = []
    filled = removed = 0
    max_rel = None
    max_abs = 0.0
    worst = None
    for key in common:
        a, b = o[key], n[key]
        if is_null(a) and is_null(b):
            continue
        if is_null(a):
            filled += 1
            continue
        if is_null(b):
            removed += 1
            continue
        if float(a) == float(b):
            continue
        d = key[0]
        revised_dates.append(d)
        ab = abs(float(b) - float(a))
        if ab > max_abs:
            max_abs = ab
        if float(a) != 0.0:
            rel = ab / abs(float(a))
            if max_rel is None or rel > max_rel:
                max_rel = rel
                worst = {"date": d, "old": float(a), "new": float(b)}
        elif worst is None:
            worst = {"date": d, "old": float(a), "new": float(b)}
    added = len(n.index.difference(o.index))
    dropped = len(o.index.difference(n.index))
    if not revised_dates and not filled and not removed and not dropped:
        return None
    revised_dates.sort()
    return {
        "revised_count": len(revised_dates),
        "added": added,
        "filled": filled,
        "removed": removed,
        "dropped": dropped,
        "range": [revised_dates[0], revised_dates[-1]] if revised_dates else None,
        "max_rel_change": round(max_rel, 6) if max_rel is not None else None,
        "max_abs_change": round(max_abs, 6),
        "worst": worst,
        "revised_dates": revised_dates[:max_dates],
        "truncated": len(revised_dates) > max_dates,
    }


def id_to_source(repo: Path) -> dict[str, str]:
    cfg = yaml.safe_load((repo / "docs" / "source_map.yaml").read_text(encoding="utf-8")) or {}
    m: dict[str, str] = {}
    for key, src in (cfg.get("sources") or {}).items():
        for ind in src.get("indicator_ids") or []:
            m[ind] = key
    return m


def source_versions(repo: Path, rev: str | None) -> dict[str, str]:
    try:
        text = git(["show", f"{rev}:data/ledger/raw-hash.json"], repo) if rev else (repo / "data/ledger/raw-hash.json").read_text(encoding="utf-8")
        d = json.loads(text)
    except Exception:  # noqa: BLE001
        return {}
    return {k: v.get("digest") for k, v in (d.get("sources") or {}).items() if isinstance(v, dict)}


def detect(repo: Path, old: str, new: str | None, max_dates: int) -> tuple[list[dict], dict]:
    paths = changed_csvs(repo, old, new)
    logger.info("changed csv files: %d (old=%s new=%s)", len(paths), old, new or "WORKTREE")
    id2src = id_to_source(repo) if (repo / "docs/source_map.yaml").exists() else {}
    versions = source_versions(repo, new)
    series = []
    for path in paths:
        o = load_rev(repo, old, path)
        n = load_rev(repo, new, path) if new else load_worktree(repo, path)
        if o is None or n is None:
            continue
        r = compare(o, n, max_dates)
        if r is None:
            continue
        ind = Path(path).stem
        src = id2src.get(ind)
        series.append({"id": ind, "source": src, "source_version": versions.get(src) if src else None, **r})
    series.sort(key=lambda s: (-(s["max_rel_change"] or 0), s["id"]))
    summary = {
        "series_revised": len(series),
        "points_revised": sum(s["revised_count"] for s in series),
        "max_rel_change": max((s["max_rel_change"] or 0) for s in series) if series else 0,
    }
    for s in series:
        logger.info("REVISED %-45s n=%-4d range=%s max_rel=%s worst=%s", s["id"], s["revised_count"], s["range"],
                    s["max_rel_change"], s["worst"])
    logger.info("summary: %s", summary)
    return series, summary


def record(ledger: dict, date: str, base: str, head: str | None, series: list[dict], summary: dict,
           retention_days: int, max_dates: int) -> dict:
    days = [d for d in ledger.get("days", []) if d.get("date") != date]
    if series:
        days.append({"date": date, "base": base, "head": head, "summary": summary, "series": series})
    cutoff = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    days = sorted([d for d in days if d["date"] >= cutoff], key=lambda d: d["date"], reverse=True)
    ledger.update({
        "schema": 1,
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "last_checked": max(date, ledger.get("last_checked") or date),
        "retention_days": retention_days,
        "max_dates_per_series": max_dates,
        "days": days,
    })
    return ledger


def nightly_commits_since(repo: Path, since: str) -> list[tuple[str, str]]:
    """main の nightly コミット（古い順）を (sha, 日付) で。日付はコミット題名「nightly: data update YYYY-MM-DD」の
    JST 日付（マシンのタイムゾーンに依らない）。since 以降のものに、その直前の 1 本を比較元として足す。"""
    out = git(["log", "--reverse", "--format=%H|%s", "--grep=^nightly: data update", "main"], repo)
    rows = []
    for line in out.splitlines():
        sha, _, subject = line.partition("|")
        m = re.search(r"nightly: data update (\d{4}-\d{2}-\d{2})", subject)
        if m:
            rows.append((sha, m.group(1)))
    idx = next((i for i, (_s, d) in enumerate(rows) if d >= since), None)
    if idx is None:
        return []
    return rows[max(0, idx - 1):]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--old", default="HEAD", help="比較元のコミット（既定 HEAD）")
    ap.add_argument("--new", default=None, help="比較先のコミット（省略時は作業ツリー）")
    ap.add_argument("--date", default=None, help="記帳する日付（既定: JST の今日）")
    ap.add_argument("--backfill-since", default=None, help="この日付以降の nightly コミットを隣り合わせて比較し、台帳を作り直す")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--max-dates", type=int, default=500)
    ap.add_argument("--retention-days", type=int, default=400)
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args(argv)
    repo: Path = args.repo
    out: Path = args.out

    if args.backfill_since:
        rows = nightly_commits_since(repo, args.backfill_since)
        if len(rows) < 2:
            logger.error("backfill: nightly commits since %s not found", args.backfill_since)
            return 1
        ledger = {"schema": 1, "days": []}
        for (old_sha, _d0), (new_sha, d1) in zip(rows, rows[1:]):
            series, summary = detect(repo, old_sha, new_sha, args.max_dates)
            ledger = record(ledger, d1, old_sha[:7], new_sha[:7], series, summary, args.retention_days, args.max_dates)
        if args.check_only:
            return 0
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("backfill: wrote %s (%d days from %d nightly commits)", out, len(ledger["days"]), len(rows))
        return 0

    date = args.date or datetime.now(JST).strftime("%Y-%m-%d")
    series, summary = detect(repo, args.old, args.new, args.max_dates)
    if args.check_only:
        return 0
    head = git(["rev-parse", "--short", args.new], repo).strip() if args.new else None
    base = git(["rev-parse", "--short", args.old], repo).strip()
    ledger = {"schema": 1, "days": []}
    if out.exists():
        ledger = json.loads(out.read_text(encoding="utf-8"))
    ledger = record(ledger, date, base, head, series, summary, args.retention_days, args.max_dates)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s (%d days, %d series on %s)", out, len(ledger["days"]), len(series), date)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        logger.exception("detect_revisions failed: %s", e)
        sys.exit(1)
