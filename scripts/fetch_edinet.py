"""
金融庁 EDINET API v2 から企業 IR（corp_ir）ドメインを seed するスクリプト（PoC）。

PoC スコープ（docs/edinet-scoping-2026-06-09.md Phase 1）:
    東京電力ホールディングス 1 社 × 主要 5 財務指標（連結・当期）を
    有価証券報告書（docTypeCode=120）から取得し catalog に載せる end-to-end 検証。

手法（2 本立て、軽量 CSV 経路）:
    1. Document List API: documents.json?date=YYYY-MM-DD&type=2
       → 提出者（edinetCode/secCode）× 有報（docTypeCode=120）で docID を動的特定。
         docID は年次でローテーションするためハードコードしない（L-062）。3 月期決算は
         6 月提出のため submission_month=6 の各日を新しい順に走査し、最新提出分を採用。
    2. Document Acquisition API: documents/{docID}?type=5
       → CSV（ZIP, TSV/UTF-16, Arelle 不要で軽量）を取得・解凍。
         メイン有報 CSV（XBRL_TO_CSV/jpcrp...asr...csv）を (要素ID, コンテキストID) で索引。

指標マップ（source_map.yaml edinet.indicators の element_id / context_id）:
    実 CSV（docID=S100W4QX, 東京電力HD 第101期 2024/04/01-2025/03/31）で要素IDを確認済（L-062）。
    連結 = base context（_NonConsolidatedMember でない方）。XBRL 単位=円 → ÷1e6 で「百万円」化。
        edinet-tepco-revenue          jppfs_cor:OperatingRevenueELE              CurrentYearDuration
        edinet-tepco-operating-income jppfs_cor:OperatingIncome                  CurrentYearDuration
        edinet-tepco-ordinary-income  jppfs_cor:OrdinaryIncome                   CurrentYearDuration
        edinet-tepco-net-income       jppfs_cor:ProfitLossAttributableToOwnersOfParent  CurrentYearDuration
        edinet-tepco-total-assets     jppfs_cor:Assets                           CurrentYearInstant

出力:
    - data/raw/edinet/{docID}_type5.zip       （取得した生 ZIP）
    - data/processed/corp_ir/{indicator_id}.csv / .parquet / .metadata.json
    long CSV: date=会計期末(periodEnd), indicator_id, region=JP, value(百万円), source_url。

API キー:
    .env の EDINET_API_KEY（python-dotenv で読み込み、無ければ os.environ）。
    Subscription-Key パラメータに渡す。キーは CSV/metadata に保存しない。

詰まったら（docID 不明・要素ID 欠落・API エラー）勝手に値を作らず停止し調査結果を報告（PoC 方針）。
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common.http import get  # noqa: E402
from scripts.common.io import append_log, save_raw, write_processed  # noqa: E402
from scripts.common.metadata import write_metadata_for_indicator  # noqa: E402

# Windows コンソール（cp932）でも日本語 print が化けないように。
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_edinet")

SOURCE_KEY = "edinet"
# XBRL 値の欠損／nil 記号（全角マイナス等）。これらは値なし扱い。
NIL_TOKENS = {"", "-", "－", "‐", "—", "NaN", "nan", "N/A", "#N/A"}


def load_source_map() -> dict:
    path = ROOT / "docs" / "source_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_api_key() -> str:
    """EDINET_API_KEY を .env（python-dotenv）→ 環境変数 の順で解決。"""
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    key = os.environ.get("EDINET_API_KEY")
    if not key:
        raise RuntimeError(
            "EDINET_API_KEY が未設定です（.env または環境変数に設定してください）"
        )
    return key


# --- Document List API: 有報 docID の動的特定 -------------------------------


def list_documents(api_base: str, day: date, key: str) -> list[dict]:
    """documents.json?date=...&type=2 の results を返す（その日の提出書類一覧）。"""
    url = f"{api_base}/documents.json"
    r = get(url, params={"date": day.isoformat(), "type": "2", "Subscription-Key": key}, timeout=60)
    r.raise_for_status()
    j = r.json()
    return j.get("results") or []


def matches_filer(doc: dict, edinet_code: str, sec_code: str | None) -> bool:
    """提出者一致判定: edinetCode 優先、無ければ secCode 前方一致。"""
    ec = (doc.get("edinetCode") or "").strip()
    if edinet_code and ec == edinet_code:
        return True
    sc = (doc.get("secCode") or "").strip()
    if sec_code and sc and sc.startswith(sec_code):
        return True
    return False


def find_asr_doc(
    api_base: str,
    key: str,
    *,
    edinet_code: str,
    sec_code: str | None,
    doc_type_code: str,
    submission_month: int,
    today: date,
    max_years: int = 2,
) -> dict | None:
    """
    対象提出者の有価証券報告書（docTypeCode=doc_type_code）の最新 docID を特定する。
    submission_month（既定 6）の各日を新しい順に走査し、最初にヒットした日（=最新提出日）の
    提出者有報を返す。未来日はスキップ。当年に無ければ前年へフォールバック（max_years）。
    """
    for y in range(today.year, today.year - max_years, -1):
        same_day_hits: list[dict] = []
        for d in range(31, 0, -1):
            try:
                day = date(y, submission_month, d)
            except ValueError:
                continue  # 6 月に 31 日は無い等
            if day > today:
                continue  # 未来は documents.json 空
            try:
                results = list_documents(api_base, day, key)
            except Exception as e:
                logger.warning("documents.json %s 取得失敗: %s（スキップ）", day, e)
                continue
            for doc in results:
                if doc.get("docTypeCode") == doc_type_code and matches_filer(doc, edinet_code, sec_code):
                    same_day_hits.append(doc)
            if same_day_hits:
                # 新しい順走査の最初の非空日 = 最新提出日。同日複数なら periodEnd 最大を採用。
                same_day_hits.sort(key=lambda x: (x.get("periodEnd") or "", x.get("docID") or ""))
                chosen = same_day_hits[-1]
                logger.info(
                    "有報 docID 特定: %s（提出日 %s, 期末 %s, %s）",
                    chosen.get("docID"), day, chosen.get("periodEnd"), chosen.get("filerName"),
                )
                return chosen
        logger.info("submission %d 年 %d 月に提出者 %s の有報なし", y, submission_month, edinet_code)
    return None


# --- Document Acquisition API: type=5 CSV(ZIP) 取得・索引 --------------------


def fetch_doc_zip(api_base: str, doc_id: str, key: str) -> bytes:
    url = f"{api_base}/documents/{doc_id}"
    logger.info("GET %s?type=5", url)
    r = get(url, params={"type": "5", "Subscription-Key": key}, timeout=180)
    r.raise_for_status()
    if len(r.content) < 1000:
        raise RuntimeError(
            f"CSV ZIP が小さすぎます（{len(r.content)} bytes）: preview={r.content[:200]!r}"
        )
    return r.content


def main_report_tsv(zip_bytes: bytes) -> pd.DataFrame:
    """ZIP から有報メイン CSV（jpcrp...asr...csv）を取り出し DataFrame で返す。"""
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    targets = [
        n for n in zf.namelist()
        if n.split("/")[-1].startswith("jpcrp") and n.endswith(".csv")
    ]
    if not targets:
        raise RuntimeError(f"ZIP に jpcrp...csv が見つかりません: {zf.namelist()}")
    name = targets[0]
    text = zf.read(name).decode("utf-16")
    df = pd.read_csv(io.StringIO(text), sep="\t", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    expected = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値"]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise RuntimeError(f"TSV 列が想定外: missing={missing}, got={list(df.columns)}")
    logger.info("main report CSV=%s rows=%d", name, len(df))
    return df


def parse_nil(raw: str | float | None) -> float | None:
    """XBRL 値文字列を数値に。nil/欠損記号は None。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s in NIL_TOKENS:
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def lookup_value(df: pd.DataFrame, element_id: str, context_id: str) -> float | None:
    """
    (要素ID, コンテキストID) 完全一致で値（円）を引く。
    複数行は全て同値なら採用、矛盾すれば例外（推測で握りつぶさない）。
    """
    sub = df[(df["要素ID"] == element_id) & (df["コンテキストID"] == context_id)]
    vals = [v for v in (parse_nil(x) for x in sub["値"]) if v is not None]
    if not vals:
        return None
    uniq = set(vals)
    if len(uniq) > 1:
        raise RuntimeError(
            f"{element_id} @ {context_id} に矛盾する複数値: {sorted(uniq)}（要調査、値を作らない）"
        )
    return vals[0]


# --- メイン ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch EDINET 有報 financials (corp_ir PoC seed)")
    parser.add_argument("--filer", default="tepco", help="source_map edinet.filers のキー（既定 tepco）")
    parser.add_argument("--doc-id", default=None, help="docID を明示指定（探索をスキップ）")
    parser.add_argument("--max-years", type=int, default=2, help="有報探索のフォールバック年数")
    parser.add_argument("--series", default=None, help="カンマ区切りで indicator_id を絞る（任意）")
    args = parser.parse_args(argv)

    cfg = load_source_map()
    try:
        source_cfg = cfg["sources"][SOURCE_KEY]
    except KeyError:
        logger.error("source_map.yaml に %s が見つかりません", SOURCE_KEY)
        return 2

    api_base = source_cfg["api_base"].rstrip("/")
    doc_type_code = str(source_cfg.get("doc_type_code", "120"))
    submission_month = int(source_cfg.get("submission_month", 6))

    filers = source_cfg.get("filers") or {}
    filer = filers.get(args.filer)
    if not filer:
        logger.error("filer %r が source_map edinet.filers にありません: %s", args.filer, list(filers))
        return 2
    edinet_code = str(filer["edinet_code"])
    sec_code = str(filer.get("sec_code") or "") or None

    indicators_cfg = source_cfg.get("indicators") or {}
    # この filer 向け指標（element_id/context_id を持つもの）。--series で更に絞る。
    wanted = {s.strip() for s in args.series.split(",")} if args.series else None
    ind_map: dict[str, tuple[str, str]] = {}
    for ind_id, meta in indicators_cfg.items():
        if "element_id" not in meta or "context_id" not in meta:
            continue
        if wanted is not None and ind_id not in wanted:
            continue
        ind_map[ind_id] = (str(meta["element_id"]), str(meta["context_id"]))
    if not ind_map:
        logger.error("対象指標が 0 件（element_id/context_id 付き indicator が無い）")
        return 2

    try:
        key = load_api_key()
    except Exception as e:
        logger.error("%s", e)
        return 2

    raw_dir = ROOT / "data" / "raw" / "edinet"
    processed_dir = ROOT / "data" / "processed" / "corp_ir"
    log_dir = ROOT / "data" / "_logs"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today = now_jst.date()

    # --- docID 特定 ---
    if args.doc_id:
        doc = {"docID": args.doc_id, "periodEnd": None, "filerName": filer.get("name_ja")}
        logger.info("docID 明示指定: %s", args.doc_id)
    else:
        doc = find_asr_doc(
            api_base, key,
            edinet_code=edinet_code, sec_code=sec_code,
            doc_type_code=doc_type_code, submission_month=submission_month,
            today=today, max_years=args.max_years,
        )
    if not doc:
        msg = f"有報 docID を特定できず（filer={args.filer}/{edinet_code}, type={doc_type_code}）"
        logger.error(msg)
        append_log(log_dir, "fetch_edinet", "FAIL", msg)
        return 1

    doc_id = doc["docID"]
    period_end = doc.get("periodEnd")
    source_url = f"{api_base}/documents/{doc_id}?type=5"

    # --- 取得・解凍・索引 ---
    try:
        zip_bytes = fetch_doc_zip(api_base, doc_id, key)
        save_raw(zip_bytes, raw_dir, f"{doc_id}_type5.zip")
        df = main_report_tsv(zip_bytes)
    except Exception as e:
        logger.exception("取得/解凍失敗: %s", e)
        append_log(log_dir, "fetch_edinet", "FAIL", f"fetch/parse failed: {e}")
        return 1

    # periodEnd が docs から取れない場合は CSV の会計期末（DEI）文字列から補完
    if not period_end:
        sub = df[df["要素ID"] == "jpdei_cor:CurrentFiscalYearEndDateDEI"]
        if len(sub):
            period_end = str(sub.iloc[0]["値"]).strip()
    if not period_end:
        msg = "会計期末（periodEnd）を特定できず、date を確定不能。停止。"
        logger.error(msg)
        append_log(log_dir, "fetch_edinet", "FAIL", msg)
        return 1

    # --- 5 指標抽出・書き出し ---
    written: list[str] = []
    results: list[tuple[str, float]] = []
    missing: list[str] = []

    for ind_id, (element_id, context_id) in sorted(ind_map.items()):
        raw_yen = lookup_value(df, element_id, context_id)
        if raw_yen is None:
            missing.append(f"{ind_id} ({element_id} @ {context_id})")
            logger.error("値なし: %s (%s @ %s)", ind_id, element_id, context_id)
            continue
        value_mn = raw_yen / 1_000_000  # 円 → 百万円
        value_out = int(value_mn) if value_mn == int(value_mn) else value_mn

        long_df = pd.DataFrame({
            "date": [period_end],
            "indicator_id": [ind_id],
            "region": ["JP"],
            "value": [value_out],
            "source_url": [source_url],
        })
        write_processed(long_df, processed_dir, basename=ind_id)
        write_metadata_for_indicator(processed_dir, source_cfg, ind_id, long_df)
        written.append(ind_id)
        results.append((ind_id, float(value_out)))
        logger.info("%s = %s 百万円 (raw=%s 円, %s @ %s)", ind_id, f"{value_out:,}", f"{int(raw_yen):,}", element_id, context_id)

    if missing:
        msg = f"要素 {len(missing)} 件欠落のため停止（値を作らない）: {missing}"
        logger.error(msg)
        append_log(log_dir, "fetch_edinet", "FAIL", msg)
        return 1
    if not written:
        logger.error("書き出した系列が 0 件")
        append_log(log_dir, "fetch_edinet", "FAIL", "no series written")
        return 1

    # --- サマリ表示（桁の目視確認用） ---
    print("=" * 64)
    print(f"[EDINET PoC] filer={args.filer} docID={doc_id} 期末={period_end} region=JP 単位=百万円")
    for ind_id, v in results:
        print(f"  {ind_id:<32} {v:>16,.0f} 百万円")
    print("=" * 64)

    summary = f"filer={args.filer} docID={doc_id} period_end={period_end} series={len(written)}"
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_edinet", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
