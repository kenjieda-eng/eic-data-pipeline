"""pytest の共通設定（2026-11-06）。リポのルートを import パスに足し、fixtures の場所を渡す。

tests/ の各ファイルは、これまで _inbox の check スクリプトで確かめてきた直し（#70〜#73）を、
PR のたびに自動で確かめ直すためのもの。ネットに出ない・鍵を読まない・data/ に触らない（一時フォルダだけ）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def fixtures_dir() -> Path:
    return ROOT / "tests" / "fixtures"
