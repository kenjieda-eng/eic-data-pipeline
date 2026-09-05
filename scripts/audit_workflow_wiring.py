#!/usr/bin/env python3
"""
scripts/audit_workflow_wiring.py — fetcher ↔ workflow 接続監査（D-020 §2.4 / ⑤-1）。

scripts/fetch_*.py を全列挙し、.github/workflows/*.yml のいずれかに
`python scripts/<name>.py` の呼び出しがあるかを照合する。

検出する故障: **未接続**（fetcher は存在するがどの workflow からも呼ばれない）。
2026-08 まで EDINET と OCCTO がこの状態で、データが静かに古くなっていた。
軸2（updated_at）は「接続はされているが失敗し続けている」を見る別担当であり、
未接続は接続監査でしか見えない（接続監査では緑に見える故障と、その逆）。

逆向きも見る: workflow が存在しない scripts/<name>.py を呼んでいる（typo・改名漏れ）。

決定論的（観測に依存しない）なので soft 期間を置かず、1 本でも違反があれば exit 1。

使い方:
    python scripts/audit_workflow_wiring.py          # 監査（違反があれば exit 1）
    python scripts/audit_workflow_wiring.py --quiet  # 違反のみ表示
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

CALL_RE = re.compile(r"python\s+scripts/([A-Za-z0-9_]+\.py)")


def collect_calls() -> dict[str, set[str]]:
    """script 名 → それを呼ぶ workflow ファイル名の集合。"""
    calls: dict[str, set[str]] = {}
    for wf in sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml")):
        text = wf.read_text(encoding="utf-8")
        for m in CALL_RE.finditer(text):
            calls.setdefault(m.group(1), set()).add(wf.name)
    return calls


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fetcher ↔ workflow wiring audit")
    ap.add_argument("--quiet", action="store_true", help="違反のみ表示")
    args = ap.parse_args(argv)

    if not WORKFLOWS_DIR.is_dir():
        print(f"ERROR: workflows dir not found: {WORKFLOWS_DIR}", file=sys.stderr)
        return 2
    fetchers = sorted(p.name for p in SCRIPTS_DIR.glob("fetch_*.py"))
    calls = collect_calls()

    unwired = [f for f in fetchers if f not in calls]
    dangling = sorted(s for s in calls if not (SCRIPTS_DIR / s).exists())

    if not args.quiet:
        print(f"wiring audit: fetchers={len(fetchers)} workflows={len(list(WORKFLOWS_DIR.glob('*.yml')))} "
              f"unwired={len(unwired)} dangling={len(dangling)}")
        for f in fetchers:
            wfs = ", ".join(sorted(calls.get(f, ()))) or "UNWIRED"
            print(f"  {f:28s} -> {wfs}")
    if unwired:
        print(f"UNWIRED fetchers ({len(unwired)}): not called by any workflow")
        for f in unwired:
            print(f"  - scripts/{f}")
    if dangling:
        print(f"DANGLING calls ({len(dangling)}): workflow calls a script that does not exist")
        for s in dangling:
            print(f"  - scripts/{s}  (called from {', '.join(sorted(calls[s]))})")
    if unwired or dangling:
        print("FAIL: wiring audit found violations.", file=sys.stderr)
        return 1
    if not args.quiet:
        print("OK: every fetcher is wired to a workflow and every workflow call resolves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
