#!/usr/bin/env python3
"""
scripts/check_fetch_outcomes.py — nightly の fetch 失敗を集約し「想定外の失敗だけ赤」にするゲート（D-020⑤-3）。

背景（Y-18, 2026-09-21）: nightly-fetch.yml の fetch ステップは continue-on-error: true で並ぶ
（1 ソースの障害で他ソースの commit を巻き込まないため）。その代償として fetch が exit 1 でも
ランは Success になり、失敗は annotation にしか残らない。FIT の AWS WAF（2026-09-05〜）は
2 週間、誰にも届かずに毎晩落ち続けた。「止まったことが見えない仕組みは止まる」。

このスクリプトは commit & push の**後**に走り、
  1. steps コンテキスト（`${{ toJSON(steps) }}` を env STEPS_JSON で受ける）から
     id 付き各ステップの outcome（continue-on-error 適用前の結果）を読み、
  2. outcome != success のうち docs/known_failures.yaml に**期限内**で登録されているものは
     「既知」として通し（理由・登録日・期限を表示）、
  3. それ以外（未登録・期限切れ）が 1 件でもあれば exit 1 でランを赤にする。

設計上の約束:
  - データは既に commit 済み。赤にしてもデータは着地している（Staleness hard check と同じ作法）。
  - allowlist には必ず期限（expires）を持たせる。期限切れは自動的に「想定外」に戻る
    （「知っていて放置していない」を期限で担保する。Y-19 §4）。
  - raw ハッシュ台帳（raw-ledger）は allowlist に載せられない
    （第 3 層が黙ると上の 2 層〈軸1・軸2〉の意味がなくなる）。
  - allowlist の step id は workflow に実在しなければならない（typo で沈黙しないため error）。
  - nightly-fetch.yml で `python scripts/fetch_*.py` / `scripts/raw_ledger.py` を呼ぶステップは
    すべて id を持ち、id はスクリプト名から機械的に決まる（fetch_boj_tankan.py → fetch-boj-tankan、
    raw_ledger.py → raw-ledger）。id が無いステップは steps コンテキストに現れずゲートの外に出るので、
    id 漏れ・規約外の id は error（沈黙の予防）。

使い方:
    CI:   env STEPS_JSON='${{ toJSON(steps) }}' → python scripts/check_fetch_outcomes.py
    手元: python scripts/check_fetch_outcomes.py --steps-json steps.json
              [--allowlist docs/known_failures.yaml] [--workflow .github/workflows/nightly-fetch.yml]
              [--today YYYY-MM-DD]
    exit 0 = 想定外の失敗なし / exit 1 = 想定外の失敗 or 設定エラー / exit 2 = 入力が読めない
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW = ROOT / ".github" / "workflows" / "nightly-fetch.yml"
DEFAULT_ALLOWLIST = ROOT / "docs" / "known_failures.yaml"

# id はスクリプト名から機械的に決まる: scripts/<stem>.py → <stem> の "_" を "-" に。
GATED_CALL_RE = re.compile(r"python\s+scripts/((?:fetch_[A-Za-z0-9_]+)|raw_ledger)\.py")
# allowlist に載せられないステップ（第 3 層）。
NEVER_ALLOWLIST = {"raw-ledger"}
REQUIRED_KEYS = ("step", "reason", "registered", "expires")
JST = timezone(timedelta(hours=9))


def _today_jst() -> date:
    return datetime.now(JST).date()


def _parse_date(value: object, label: str, errors: list[str]) -> date | None:
    """YAML は日付リテラルを date に、引用付きは str にする。両方受ける。"""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    errors.append(f"{label}: invalid date {value!r} (expected YYYY-MM-DD)")
    return None


def expected_ids_from_workflow(path: Path, errors: list[str]) -> list[str]:
    """nightly-fetch.yml の fetch / ledger ステップの id を規約どおりに集める。規約違反は errors へ。"""
    try:
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        errors.append(f"workflow: cannot parse {path}: {e}")
        return []
    steps = (((wf or {}).get("jobs") or {}).get("fetch") or {}).get("steps") or []
    ids: list[str] = []
    for step in steps:
        run = step.get("run") if isinstance(step, dict) else None
        if not isinstance(run, str):
            continue
        stems = sorted(set(GATED_CALL_RE.findall(run)))
        if not stems:
            continue
        name = step.get("name", "?")
        if len(stems) != 1:
            errors.append(f"workflow: step {name!r} calls {len(stems)} gated scripts {stems}; one script per step")
            continue
        want = stems[0].replace("_", "-")
        got = step.get("id")
        if not got:
            errors.append(f"workflow: step {name!r} calls scripts/{stems[0]}.py but has no id (expected id: {want})")
            continue
        if got != want:
            errors.append(f"workflow: step {name!r} has id {got!r} but the convention requires {want!r}")
            continue
        if step.get("continue-on-error") is not True:
            # continue-on-error が無いと失敗時点で job が止まり、このゲートまで来ない（= 従来どおり赤）。
            # 規約としては付いている前提なので、外れていたら気付けるように error にする。
            errors.append(f"workflow: step {name!r} (id {got}) lacks continue-on-error: true")
            continue
        ids.append(got)
    if not ids and not errors:
        errors.append(f"workflow: no gated steps found in {path}")
    return ids


def load_allowlist(path: Path, expected_ids: list[str], today: date, errors: list[str]) -> list[dict]:
    """known_failures.yaml を読んで検証する。戻り値は正規化済み項目（step/reason/registered/expires/ref）。"""
    if not path.exists():
        errors.append(f"allowlist: {path} not found (an empty list is fine, a missing file is not)")
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        errors.append(f"allowlist: cannot parse {path}: {e}")
        return []
    raw = doc.get("known_failures") if isinstance(doc, dict) else None
    if raw is None:
        errors.append("allowlist: top-level key 'known_failures' is required (use [] for none)")
        return []
    if not isinstance(raw, list):
        errors.append("allowlist: 'known_failures' must be a list")
        return []
    entries: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        label = f"allowlist[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: must be a mapping")
            continue
        missing = [k for k in REQUIRED_KEYS if not item.get(k)]
        if missing:
            errors.append(f"{label}: missing required key(s) {missing}")
            continue
        step = str(item["step"]).strip()
        if step in NEVER_ALLOWLIST:
            errors.append(f"{label}: step {step!r} cannot be allowlisted (third layer must stay loud)")
            continue
        if step not in expected_ids:
            errors.append(f"{label}: step {step!r} is not a gated step id in the workflow (typo? known ids: {expected_ids})")
            continue
        if step in seen:
            errors.append(f"{label}: duplicate entry for step {step!r}")
            continue
        registered = _parse_date(item["registered"], f"{label}.registered", errors)
        expires = _parse_date(item["expires"], f"{label}.expires", errors)
        if registered is None or expires is None:
            continue
        if expires < registered:
            errors.append(f"{label}: expires {expires} is before registered {registered}")
            continue
        if registered > today:
            errors.append(f"{label}: registered {registered} is in the future (today={today})")
            continue
        seen.add(step)
        entries.append({
            "step": step,
            "reason": str(item["reason"]).strip(),
            "registered": registered,
            "expires": expires,
            "ref": str(item.get("ref") or "").strip(),
        })
    return entries


def load_steps(args: argparse.Namespace) -> dict | None:
    if args.steps_json:
        text = Path(args.steps_json).read_text(encoding="utf-8")
    else:
        text = os.environ.get("STEPS_JSON", "")
        if not text.strip():
            print("ERROR: STEPS_JSON env is empty and --steps-json not given", file=sys.stderr)
            return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"ERROR: steps JSON is not valid JSON: {e}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print("ERROR: steps JSON must be an object keyed by step id", file=sys.stderr)
        return None
    return data


def write_summary(lines: list[str]) -> None:
    """GITHUB_STEP_SUMMARY があれば Markdown で追記（無ければ何もしない）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Fetch outcome gate: unexpected fetch failures → exit 1 (D-020⑤-3)")
    ap.add_argument("--steps-json", help="toJSON(steps) を保存したファイル（省略時は env STEPS_JSON）")
    ap.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST), help="docs/known_failures.yaml")
    ap.add_argument("--workflow", default=str(DEFAULT_WORKFLOW), help=".github/workflows/nightly-fetch.yml")
    ap.add_argument("--today", help="判定日 YYYY-MM-DD（省略時は JST の今日）")
    args = ap.parse_args(argv)

    errors: list[str] = []
    today = date.fromisoformat(args.today) if args.today else _today_jst()

    expected_ids = expected_ids_from_workflow(Path(args.workflow), errors)
    allow = load_allowlist(Path(args.allowlist), expected_ids, today, errors)
    steps = load_steps(args)
    if steps is None:
        return 2

    # 期待した id が steps コンテキストに無い = workflow の id と実行の食い違い（ゲートの外に出ている）。
    for sid in expected_ids:
        if sid not in steps:
            errors.append(f"steps: gated step id {sid!r} is absent from the steps context")

    by_step = {e["step"]: e for e in allow}
    known: list[tuple[str, str, dict]] = []      # (id, outcome, entry)
    unexpected: list[tuple[str, str, str]] = []  # (id, outcome, why)
    ok: list[str] = []
    for sid in expected_ids:
        info = steps.get(sid) or {}
        outcome = str(info.get("outcome") or "missing")
        if outcome == "success":
            ok.append(sid)
            continue
        if outcome == "skipped":
            # if: で飛ばされた（現状の nightly には無い）。赤にはしないが見えるようにする。
            print(f"NOTE: {sid}: outcome=skipped (not gating)")
            continue
        entry = by_step.get(sid)
        if entry is None:
            unexpected.append((sid, outcome, "not in known_failures.yaml"))
        elif today > entry["expires"]:
            unexpected.append((sid, outcome, f"known_failures entry expired on {entry['expires']} (registered {entry['registered']})"))
        else:
            known.append((sid, outcome, entry))

    recovered = [e for e in allow if e["step"] in ok]

    # ---- 出力 ------------------------------------------------------------------
    print(f"FETCH OUTCOME GATE (D-020⑤-3): today={today}, gated steps={len(expected_ids)}, "
          f"ok={len(ok)}, known={len(known)}, unexpected={len(unexpected)}, config errors={len(errors)}")
    md = ["## Fetch outcome gate (D-020⑤-3)", "",
          f"gated steps: **{len(expected_ids)}** / ok {len(ok)} / known {len(known)} / "
          f"unexpected **{len(unexpected)}** / config errors **{len(errors)}** (today {today})", ""]
    if known:
        print(f"KNOWN FAILURES (allowlisted, not gating): {len(known)}")
        md += ["### Known failures (allowlisted)", "", "| step | outcome | registered | expires | reason |", "|---|---|---|---|---|"]
        for sid, outcome, e in known:
            left = (e["expires"] - today).days
            print(f"  - {sid}: outcome={outcome}; registered {e['registered']}, expires {e['expires']} ({left}d left)"
                  f"{' [' + e['ref'] + ']' if e['ref'] else ''}\n      reason: {e['reason']}")
            md.append(f"| {sid} | {outcome} | {e['registered']} | {e['expires']} ({left}d left) | {e['reason']} |")
        md.append("")
    if recovered:
        print(f"NOTE: allowlisted but succeeded (recovered? remove the entry when stable): "
              + ", ".join(e["step"] for e in recovered))
        md += ["Allowlisted but succeeded: " + ", ".join(f"`{e['step']}`" for e in recovered), ""]
    if unexpected:
        print(f"UNEXPECTED FAILURES (gating): {len(unexpected)}")
        md += ["### Unexpected failures (gating)", "", "| step | outcome | why |", "|---|---|---|"]
        for sid, outcome, why in unexpected:
            print(f"  - {sid}: outcome={outcome}; {why}")
            md.append(f"| {sid} | {outcome} | {why} |")
        md.append("")
    if errors:
        print(f"CONFIG ERRORS (gating): {len(errors)}")
        md += ["### Config errors (gating)", ""]
        for e in errors:
            print(f"  - {e}")
            md.append(f"- {e}")
        md.append("")

    if unexpected or errors:
        print("FAIL: unexpected fetch failure(s) and/or config error(s). Data (if any) is already committed; "
              "fix the source, or register a known failure WITH an expiry in docs/known_failures.yaml.")
        md.append("**FAIL** — see above.")
        write_summary(md)
        return 1
    print("OK: no unexpected fetch failures.")
    md.append("**OK** — no unexpected fetch failures.")
    write_summary(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
