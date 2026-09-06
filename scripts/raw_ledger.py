#!/usr/bin/env python3
"""
scripts/raw_ledger.py — raw ハッシュ台帳（D-020⑤-1）。

data/raw/<dir>/ の「現在の内容」を毎晩ハッシュして台帳（data/ledger/raw-hash.json）に
記帳し、同じ台帳から **二つの顔** を判定する:

  凍結検知（freeze）: 変わるはずの raw が N 日を超えて 1 バイトも変わらない
      → L-064 型サイレント停滞（Pink Sheet の URL 年度切替、Ember の URL 変更、
        enecho の cache 短絡）を、軸1（observation_cutoff の SLA）より早く、
        国別 SLA の段差にも依存せずソース単位で捉える。
  変化通知（change）: 過去値が改訂されうる raw が変わった日を知らせる
      → 派生系列の再計算は同 run 内で済むが、記事に引用済みの数値が静かに
        変わる「告知問題」（Ember 6 記事、EIA の GDP 分母改訂 2026-08-25）を
        人間の L-077 三系統 grep に接続する。

三層の語彙: 軸1 = データの日付 / 軸2 = workflow の生存 / **第3層 = 上流の発行そのもの**。
本スクリプトは第3層を担当する。

設計（2026-09-05、リン）:
  - フェッチ時（save_raw）に記帳する案は採らない。cache hit のように **ダウンロードが
    起きない夜は記帳も起きず**、enecho 型（第 3 例）を見逃す。かわりに全 fetch step の
    後で data/raw を **走査** し、「リポジトリが今持っている raw」を測る（post-hoc scan）。
    これなら fetch の成否・cache の有無・annual workflow（edinet/occto）の別経路に
    依存せず、processed の元になった実体そのものを見る。
  - ファイル名の末尾 `_YYYYMMDD` は取得日トークン（save_raw の today_tag）として剥がし、
    同じ stem を 1 つの key にまとめる。key の「現在」= 最大日付のファイル。
    日付トークンを持たないファイル（enecho の年度 XLSX、jepx の年別 CSV、gio、edinet）は
    その名前がそのまま key（上書き型）。
  - ソースの digest = 全 key の (key, sha256) を並べたものの sha256。
    digest が変わらない限り history の最後のエントリの last_seen を進めるだけ
    （run-length 記録）。変わったら新エントリを first_seen=today で追加する。
  - 初回記帳（seed）: 日付トークン型は同 key の過去ファイルを日付降順にたどり、
    sha256 が同一である最古の日付を first_seen とする。上書き型は
    `git log -1 --format=%cs -- <file>` の日付（取れなければ today）。
    ⚠️ CI の shallow checkout では git の日付が today になる。seed はローカルの
    フルクローンで一度行い、生成した台帳を PR に含めること。
  - テキストファイル（先頭 8KB に NUL 無し）は CR を除いてハッシュする（2026-09-06:
    Windows checkout の autocrlf で seed と CI のハッシュが食い違い、初日に偽「変化」が
    14 ソースで出た。行末は上流の発行内容ではない）。
  - 判定は docs/source_map.yaml の各ソース `raw_watch:` 宣言に従う:
        raw_watch:
          dir: fuel            # data/raw/<dir>
          freeze_days: 40      # null なら凍結判定しない（annual 系）
          notify_change: true  # 変わった日に通知する
    宣言の無い dir も **記帳はする**（判定しない）。後から宣言を足せば履歴が既にある。
  - ⑤-1 では report-only（exit 0）。hard 化は D-020⑤-2 の昇格条件
    「各判定ごとに soft 運転 14 nightly 連続で誤検知 0」を満たしてから。

使い方:
    python scripts/raw_ledger.py                 # 記帳 + 判定（nightly の fetch 群の後・catalog 生成の前）
    python scripts/raw_ledger.py --dry-run       # 台帳を書かずに判定だけ表示
    python scripts/raw_ledger.py --today 2026-09-07   # 日付を固定（テスト用）
    python scripts/raw_ledger.py --root PATH --ledger PATH --source-map PATH

出力（check_staleness.py が台帳の assessment を読んで同じ内容を再掲する）:
    raw ledger: ...
    RAW_FREEZE (report-only): N source(s)
      - <dir>: unchanged Xd > freeze_days=Nd since YYYY-MM-DD
    RAW_CHANGED: N source(s)
      - <dir>: digest changed today (previous stable since YYYY-MM-DD, Xd); keys: ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml  # PyYAML（requirements.txt 済み）
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_ROOT = ROOT / "data" / "raw"
DEFAULT_LEDGER = ROOT / "data" / "ledger" / "raw-hash.json"
DEFAULT_SOURCE_MAP = ROOT / "docs" / "source_map.yaml"

SCHEMA = "raw-ledger-v1"
HISTORY_KEEP = 60          # 1 dir あたり保持する digest 変化履歴の上限
SEED_WALK_LIMIT = 200      # seed 時に同 key の過去ファイルをたどる上限
KEYS_STORE_LIMIT = 50      # 監視宣言の無い dir で key 別 sha256 を保存する key 数の上限
# git 非追跡（.gitignore）の raw。nightly の checkout に無く、ローカルにだけあるので
# 記帳すると環境差で digest が揺れる。年版不変・S3 再取得可の NREL ATB。
EXCLUDE_DIRS = {"nrel-atb"}

# ファイル名末尾の取得日トークン: <key>_YYYYMMDD.<ext>
DATE_TOKEN_RE = re.compile(r"^(?P<stem>.+?)_(?P<date>20\d{6})(?P<ext>\.[A-Za-z0-9]+)$")


def _now_jst() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))


TEXT_SNIFF_BYTES = 8192


def _is_text_file(path: Path) -> bool:
    """先頭 8KB に NUL が無ければテキスト扱い（CSV / JSON / HTML）。xlsx / zip は NUL を含む。"""
    with path.open("rb") as f:
        head = f.read(TEXT_SNIFF_BYTES)
    return b"\x00" not in head


def _sha256_file(path: Path) -> str:
    """ファイル内容の sha256。テキストは CR（0x0D）を除いてからハッシュする。

    2026-09-06 の教訓: 台帳の seed を Windows の checkout（core.autocrlf=true）で行ったため、
    LF のテキスト raw が作業ツリーでは CRLF になっており、CI（Linux）の初回記帳で
    テキスト系 14 ソースが一斉に「変化」した（バイナリの xlsx / zip は一致）。
    行末は上流の発行内容ではないので、環境に依存しないよう CR を無視してハッシュする。
    """
    h = hashlib.sha256()
    text = _is_text_file(path)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk.replace(b"\r", b"") if text else chunk)
    return h.hexdigest()


def _token_date(tok: str) -> str:
    return f"{tok[0:4]}-{tok[4:6]}-{tok[6:8]}"


def split_key(filename: str) -> tuple[str, str | None]:
    """'ember_monthly_20260905.csv' → ('ember_monthly.csv', '2026-09-05')。
    日付トークンが無ければ (filename, None)。"""
    m = DATE_TOKEN_RE.match(filename)
    if not m:
        return filename, None
    return f"{m.group('stem')}{m.group('ext')}", _token_date(m.group("date"))


def scan_dir(dir_path: Path) -> dict[str, dict]:
    """dir 内のファイルを key ごとにまとめ、各 key の「現在」（最大日付 or 唯一）を返す。
    戻り値: key → {file, sha256, size, date, siblings}（siblings は同 key の他ファイル、日付降順）"""
    groups: dict[str, list[tuple[str | None, Path]]] = {}
    for p in sorted(dir_path.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        key, d = split_key(p.name)
        groups.setdefault(key, []).append((d, p))
    result: dict[str, dict] = {}
    for key, items in groups.items():
        # 日付付き key は日付降順に並べ、先頭が「現在」。上書き型は 1 件のみ。
        dated = sorted((t for t in items if t[0] is not None), key=lambda t: t[0], reverse=True)
        if dated:
            cur_date, cur = dated[0]
            siblings = [p for _, p in dated[1:]]
        else:
            cur_date, cur = items[0]
            siblings = [p for _, p in items[1:]]
        result[key] = {
            "file": cur.name,
            "sha256": _sha256_file(cur),
            "size": cur.stat().st_size,
            "date": cur_date,
            "_siblings": siblings,
        }
    return result


def source_digest(keys: dict[str, dict]) -> str:
    lines = "\n".join(f"{k}\t{v['sha256']}" for k, v in sorted(keys.items()))
    return "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _git_last_change_date(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", str(path)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        s = out.stdout.strip()
        return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None
    except Exception:  # noqa: BLE001
        return None


def seed_first_seen(keys: dict[str, dict], today: date) -> str:
    """初回記帳の first_seen を推定する（docstring「seed」参照）。
    dir の first_seen = 各 key の first_seen の最大値（最後に変わった key の日付）。"""
    firsts: list[str] = []
    for key, info in keys.items():
        if info["date"] is not None:
            first = info["date"]
            n = 0
            for sib in info["_siblings"]:
                if n >= SEED_WALK_LIMIT:
                    break
                n += 1
                if _sha256_file(sib) != info["sha256"]:
                    break
                _, d = split_key(sib.name)
                if d:
                    first = d
            firsts.append(first)
        else:
            d = _git_last_change_date(Path("data") / "raw" / info["_dir"] / info["file"])
            firsts.append(d or today.isoformat())
    return max(firsts) if firsts else today.isoformat()


def load_watch(source_map_path: Path) -> dict[str, dict]:
    """source_map.yaml の raw_watch 宣言を dir → {source, freeze_days, notify_change} に。"""
    if yaml is None or not source_map_path.exists():
        return {}
    cfg = yaml.safe_load(source_map_path.read_text(encoding="utf-8")) or {}
    out: dict[str, dict] = {}
    for src, body in (cfg.get("sources") or {}).items():
        if not isinstance(body, dict):
            continue
        w = body.get("raw_watch")
        if not isinstance(w, dict) or not w.get("dir"):
            continue
        out[str(w["dir"])] = {
            "source": src,
            "freeze_days": w.get("freeze_days"),
            "notify_change": bool(w.get("notify_change", False)),
        }
    return out


def load_ledger(path: Path) -> dict:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise SystemExit(f"ERROR: unexpected ledger schema {data.get('schema')!r} in {path}")
        return data
    return {"schema": SCHEMA, "updated_at": None, "sources": {}, "assessment": None}


def update_ledger(ledger: dict, raw_root: Path, today: date, watch: dict[str, dict] | None = None) -> dict[str, dict]:
    """data/raw を走査して ledger["sources"] を更新。dir → {changed, changed_keys, stable_since, days, n_keys, prev_since}

    key ごとの sha256 は、監視宣言のある dir か key 数が KEYS_STORE_LIMIT 以下の dir にだけ保存する
    （jma は月別 HTML が 1,500 超あり、全 dir で持つと台帳が肥大する。digest と履歴は全 dir で持つ）。"""
    watch = watch or {}
    result: dict[str, dict] = {}
    for dir_path in sorted(p for p in raw_root.iterdir() if p.is_dir()):
        name = dir_path.name
        if name in EXCLUDE_DIRS or name.startswith("."):
            continue
        keys = scan_dir(dir_path)
        if not keys:
            continue
        for k in keys.values():
            k["_dir"] = name
        digest = source_digest(keys)
        entry = ledger["sources"].get(name)
        changed = False
        changed_keys: list[str] = []
        if entry is None:
            first = seed_first_seen(keys, today)
            entry = {"digest": digest, "stable_since": first, "last_seen": today.isoformat(),
                     "keys": {}, "history": [{"digest": digest, "first_seen": first,
                                              "last_seen": today.isoformat()}]}
            ledger["sources"][name] = entry
        else:
            last = entry["history"][-1]
            if last["digest"] == digest:
                last["last_seen"] = today.isoformat()
            else:
                prev_keys = entry.get("keys")
                changed = True
                if prev_keys is not None:
                    for k in sorted(set(prev_keys) | set(keys)):
                        a = (prev_keys.get(k) or {}).get("sha256")
                        b = (keys.get(k) or {}).get("sha256")
                        if a != b:
                            changed_keys.append(k)
                entry["history"].append({"digest": digest, "first_seen": today.isoformat(),
                                         "last_seen": today.isoformat()})
                entry["history"] = entry["history"][-HISTORY_KEEP:]
            entry["digest"] = digest
            entry["stable_since"] = entry["history"][-1]["first_seen"]
            entry["last_seen"] = today.isoformat()
        if name in watch or len(keys) <= KEYS_STORE_LIMIT:
            entry["keys"] = {k: {"file": v["file"], "sha256": v["sha256"], "size": v["size"]}
                             for k, v in sorted(keys.items())}
        else:
            entry["keys"] = None  # digest と履歴のみ（key 別 sha は省略）
        stable_since = date.fromisoformat(entry["stable_since"])
        result[name] = {
            "changed": changed,
            "changed_keys": changed_keys,
            "stable_since": entry["stable_since"],
            "days": (today - stable_since).days,
            "n_keys": len(keys),
            "prev_since": entry["history"][-2]["first_seen"] if changed and len(entry["history"]) >= 2 else None,
        }
    return result


def assess(result: dict[str, dict], watch: dict[str, dict], today: date) -> dict:
    frozen: list[dict] = []
    changed: list[dict] = []
    for d, w in sorted(watch.items()):
        r = result.get(d)
        if r is None:
            frozen.append({"dir": d, "source": w["source"], "missing": True})
            continue
        fd = w.get("freeze_days")
        if isinstance(fd, (int, float)) and fd > 0 and r["days"] > fd:
            frozen.append({"dir": d, "source": w["source"], "days": r["days"],
                           "freeze_days": int(fd), "stable_since": r["stable_since"]})
        if w.get("notify_change") and r["changed"]:
            prev_since = r["prev_since"]
            prev_days = (today - date.fromisoformat(prev_since)).days if prev_since else None
            changed.append({"dir": d, "source": w["source"], "keys": r["changed_keys"],
                            "previous_since": prev_since, "previous_days": prev_days})
    return {"date": today.isoformat(), "watched": sorted(watch), "frozen": frozen, "changed": changed}


def format_assessment(a: dict) -> list[str]:
    lines: list[str] = []
    fr = a.get("frozen") or []
    ch = a.get("changed") or []
    lines.append(f"RAW_FREEZE (report-only): {len(fr)} source(s)")
    for f in fr:
        if f.get("missing"):
            lines.append(f"  - {f['dir']} ({f['source']}): raw dir missing — nothing fetched?")
        else:
            lines.append(f"  - {f['dir']} ({f['source']}): unchanged {f['days']}d > freeze_days={f['freeze_days']}d since {f['stable_since']}")
    lines.append(f"RAW_CHANGED: {len(ch)} source(s)")
    for c in ch:
        prev = f"previous stable since {c['previous_since']}, {c['previous_days']}d" if c.get("previous_since") else "previous unknown"
        keys = ", ".join(c["keys"][:8]) + (" …" if len(c["keys"]) > 8 else "")
        lines.append(f"  - {c['dir']} ({c['source']}): digest changed today ({prev}); keys: {keys} → L-077: grep cited values")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="raw hash ledger (D-020⑤-1): record + assess")
    ap.add_argument("--root", type=Path, default=DEFAULT_RAW_ROOT, help="data/raw のパス")
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="台帳 JSON のパス")
    ap.add_argument("--source-map", type=Path, default=DEFAULT_SOURCE_MAP, help="docs/source_map.yaml")
    ap.add_argument("--today", type=str, default=None, help="YYYY-MM-DD（既定: 今日 JST）")
    ap.add_argument("--dry-run", action="store_true", help="台帳を書かない")
    args = ap.parse_args(argv)

    if not args.root.is_dir():
        print(f"ERROR: raw root not found: {args.root}", file=sys.stderr)
        return 2
    today = date.fromisoformat(args.today) if args.today else _now_jst().date()
    ledger = load_ledger(args.ledger)
    watch = load_watch(args.source_map)
    result = update_ledger(ledger, args.root, today, watch)
    a = assess(result, watch, today)
    ledger["assessment"] = a
    ledger["updated_at"] = _now_jst().isoformat(timespec="seconds")

    print(f"raw ledger: root={args.root} dirs={len(result)} watched={len(watch)} "
          f"today(JST)={today} ledger={args.ledger}{' (dry-run)' if args.dry_run else ''}")
    for d, r in sorted(result.items()):
        w = watch.get(d)
        tag = ""
        if w:
            parts = []
            if w.get("freeze_days"):
                parts.append(f"freeze>{w['freeze_days']}d")
            if w.get("notify_change"):
                parts.append("notify")
            tag = f"  [watch: {', '.join(parts) or 'record-only'}]"
        flag = " CHANGED" if r["changed"] else ""
        print(f"  {d:16s} stable_since={r['stable_since']} ({r['days']:>3}d) keys={r['n_keys']:<3}{tag}{flag}")
    for line in format_assessment(a):
        print(line)

    if not args.dry_run:
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        args.ledger.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
