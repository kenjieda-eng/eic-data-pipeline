"""
金融庁 EDINET API v2 から企業 IR（corp_ir）ドメインを seed するスクリプト。

スコープ（docs/edinet-scoping-2026-06-09.md）:
    Phase 1 (PoC, PR #18): 東京電力HD 1 社 × 主要 5 指標（連結・当期 1 点）。
    Phase 2 (本 seed):     9 一般電気事業者 × 主要 5 指標（連結）を直近 ~10 年度 backfill。
        9 社 = tepco/chuden/kepco/energia/rikuden/tohoku/yonden/kyuden/hepco
        （edinetCode は EDINET コードリスト Edinetcode.zip で確定, L-062）。

手法（2 本立て、軽量 CSV 経路）:
    1. Document List API: documents.json?date=YYYY-MM-DD&type=2
       → 提出年（submission_month=6 の各日を新しい順）を 1 パス走査し、対象 9 社の
         有報（docTypeCode=120）docID をまとめて動的特定（build_year_index）。
         docID は年次ローテーションするためハードコードしない（L-062）。
    2. Document Acquisition API: documents/{docID}?type=5
       → CSV（ZIP, TSV/UTF-16, Arelle 不要で軽量）を取得・解凍。
         メイン有報 CSV（XBRL_TO_CSV/jpcrp...asr...csv）を (要素ID, コンテキストID) で索引。
         各有報の CurrentYear コンテキストを採用 → 1 有報 = 1 会計年度分の値。

指標マップ（source_map.yaml edinet.indicators の element_id / context_id / fallback_element_ids）:
    全 9 社・全年度 JGAAP・連結ベースで同一要素を probe 確認済（2026-06-13, L-062）。
    連結 = base context（_NonConsolidatedMember でない方）。XBRL 単位=円 → ÷1e6 で「百万円」化。
        *-revenue          jppfs_cor:OperatingRevenueELE              CurrentYearDuration
        *-operating-income jppfs_cor:OperatingIncome                  CurrentYearDuration
        *-ordinary-income  jppfs_cor:OrdinaryIncome                   CurrentYearDuration
                           （IFRS 適用時 → jppfs_cor:IncomeBeforeIncomeTaxes へ fallback）
        *-net-income       jppfs_cor:ProfitLossAttributableToOwnersOfParent  CurrentYearDuration
        *-total-assets     jppfs_cor:Assets                           CurrentYearInstant

出力:
    - data/raw/edinet/{docID}_type5.zip       （取得した生 ZIP, 再実行時はキャッシュ）
    - data/processed/corp_ir/{indicator_id}.csv / .parquet / .metadata.json
    long CSV: date=会計期末(periodEnd) を年度ごとに 1 行, indicator_id, region=JP, value(百万円), source_url。
    write_processed は append-safe（date×indicator×region でユニーク化）なので増分 backfill 可。

API キー:
    .env の EDINET_API_KEY（python-dotenv で読み込み、無ければ os.environ）。
    Subscription-Key パラメータに渡す。キーは CSV/metadata に保存しない。

詰まったら（docID 不明・要素ID 欠落・API エラー）勝手に値を作らず、取れた社数/年度で報告。
backfill では (社×年度×指標) 単位の欠落は skip+ログし、取得できた分を書き出して継続する。
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import zipfile
from collections import defaultdict
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
    単一提出者の有価証券報告書（docTypeCode=doc_type_code）の最新 docID を特定する。
    submission_month（既定 6）の各日を新しい順に走査し、最初にヒットした日（=最新提出日）の
    提出者有報を返す。未来日はスキップ。当年に無ければ前年へフォールバック（max_years）。
    （単一社・最新 1 点用。複数社 backfill は build_year_index を使う。）
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


def build_year_index(
    api_base: str,
    key: str,
    target_codes: set[str],
    *,
    year: int,
    submission_month: int,
    doc_type_code: str,
    today: date,
) -> dict[str, dict]:
    """
    指定提出年（submission_month の各日）を新しい順に 1 パス走査し、対象 edinetCode 集合の
    有報（docTypeCode）を {edinetCode: doc} でまとめて返す。複数社 backfill 用に
    documents.json の呼び出しを年単位で共有する（社ごと走査だと 9 倍叩いてしまうため）。
    同一社が複数日に出す場合は新しい日（先にヒットした日）を採用。全社揃ったら早期終了。
    """
    idx: dict[str, dict] = {}
    for d in range(31, 0, -1):
        try:
            day = date(year, submission_month, d)
        except ValueError:
            continue
        if day > today:
            continue
        try:
            results = list_documents(api_base, day, key)
        except Exception as e:
            logger.warning("documents.json %s 取得失敗: %s（スキップ）", day, e)
            continue
        for doc in results:
            if doc.get("docTypeCode") != doc_type_code:
                continue
            ec = (doc.get("edinetCode") or "").strip()
            if ec in target_codes and ec not in idx:
                idx[ec] = doc
        if len(idx) == len(target_codes):
            break
    return idx


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


def get_doc_zip_cached(api_base: str, doc_id: str, key: str, raw_dir: Path) -> bytes:
    """raw_dir に {doc_id}_type5.zip があれば再利用、無ければ取得して保存。"""
    cache = raw_dir / f"{doc_id}_type5.zip"
    if cache.exists() and cache.stat().st_size >= 1000:
        logger.info("cache hit: %s", cache.name)
        return cache.read_bytes()
    zip_bytes = fetch_doc_zip(api_base, doc_id, key)
    save_raw(zip_bytes, raw_dir, f"{doc_id}_type5.zip")
    return zip_bytes


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


def lookup_value_chain(
    df: pd.DataFrame, element_id: str, context_id: str, fallbacks: list[str]
) -> tuple[float | None, str | None]:
    """
    まず primary element_id を引き、None なら fallback_element_ids を順に試す。
    （IFRS 適用社の経常利益→税引前当期純利益フォールバック等。当 9 社は全年度 JGAAP で未発火想定）。
    戻り値: (値[円] or None, 実際に採用した element_id or None)。
    """
    v = lookup_value(df, element_id, context_id)
    if v is not None:
        return v, element_id
    for fb in fallbacks:
        v = lookup_value(df, fb, context_id)
        if v is not None:
            return v, fb
    return None, None


# --- 指標マップ構築 ----------------------------------------------------------


def build_ind_map(
    indicators_cfg: dict, alias: str, wanted: set[str] | None
) -> dict[str, tuple[str, str, list[str]]]:
    """
    filer alias 向け指標 {ind_id: (element_id, context_id, fallback_element_ids)} を返す。
    ind_id は edinet-{alias}-{metric} 命名（element_id/context_id を持つもののみ）。
    """
    prefix = f"edinet-{alias}-"
    m: dict[str, tuple[str, str, list[str]]] = {}
    for ind_id, meta in indicators_cfg.items():
        if not ind_id.startswith(prefix):
            continue
        if not isinstance(meta, dict) or "element_id" not in meta or "context_id" not in meta:
            continue
        if wanted is not None and ind_id not in wanted:
            continue
        fb = meta.get("fallback_element_ids") or []
        m[ind_id] = (str(meta["element_id"]), str(meta["context_id"]), [str(x) for x in fb])
    return m


def to_value_mn(raw_yen: float) -> float | int:
    """円 → 百万円。整数になるなら int で返す（CSV を綺麗に）。"""
    value_mn = raw_yen / 1_000_000
    return int(value_mn) if value_mn == int(value_mn) else value_mn


def period_end_from_df(df: pd.DataFrame) -> str | None:
    """docs から periodEnd が取れない場合に DEI の会計期末を補完。"""
    sub = df[df["要素ID"] == "jpdei_cor:CurrentFiscalYearEndDateDEI"]
    if len(sub):
        s = str(sub.iloc[0]["値"]).strip()
        if s and s not in NIL_TOKENS:
            return s
    return None


# --- メイン ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch EDINET 有報 financials (corp_ir seed, Phase 2 = 9電力)")
    parser.add_argument("--filer", default="all", help="'all'（既定）または edinet.filers のキーをカンマ区切り（例 tepco,kepco）")
    parser.add_argument("--backfill-years", type=int, default=10, help="さかのぼる提出年数（既定 10。当年は未提出で空でも可）")
    parser.add_argument("--series", default=None, help="カンマ区切りで indicator_id を絞る（任意）")
    parser.add_argument("--doc-id", default=None, help="単一 docID を明示指定（単一 filer・デバッグ用、backfill しない）")
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
    indicators_cfg = source_cfg.get("indicators") or {}

    # --- 対象 filer の解決 ---
    if args.filer.strip().lower() == "all":
        aliases = list(filers)
    else:
        aliases = [a.strip() for a in args.filer.split(",") if a.strip()]
    bad = [a for a in aliases if a not in filers]
    if bad:
        logger.error("未知の filer: %s（既知: %s）", bad, list(filers))
        return 2
    if not aliases:
        logger.error("対象 filer が 0 件")
        return 2

    wanted = {s.strip() for s in args.series.split(",")} if args.series else None
    ind_maps = {a: build_ind_map(indicators_cfg, a, wanted) for a in aliases}
    aliases = [a for a in aliases if ind_maps[a]]  # 指標が無い filer は除外
    if not aliases:
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

    code_to_alias = {filers[a]["edinet_code"]: a for a in aliases}
    target_codes = set(code_to_alias)

    rows_by_ind: dict[str, list[dict]] = defaultdict(list)
    coverage: dict[str, list[str]] = defaultdict(list)   # alias -> [period_end...]
    missing: list[str] = []
    fallbacks_used: list[str] = []
    docs_processed = 0

    # --- 単一 docID デバッグ経路（backfill しない） ---
    if args.doc_id:
        if len(aliases) != 1:
            logger.error("--doc-id は単一 filer 指定時のみ（--filer に 1 社を指定）")
            return 2
        alias = aliases[0]
        index = {filers[alias]["edinet_code"]: {"docID": args.doc_id, "periodEnd": None}}
        scan_plan = [("(explicit)", index)]
    else:
        # 提出年を新しい順に走査（当年は未提出で空でも可。backfill_years+1 年窓で ~N 完全年度を確保）。
        scan_plan = []
        for year in range(today.year, today.year - args.backfill_years - 1, -1):
            idx = build_year_index(
                api_base, key, target_codes,
                year=year, submission_month=submission_month,
                doc_type_code=doc_type_code, today=today,
            )
            logger.info("提出年 %d: 有報 %d/%d 社ヒット", year, len(idx), len(target_codes))
            if idx:
                scan_plan.append((str(year), idx))

    # --- 各有報を取得・抽出 ---
    for label, idx in scan_plan:
        for ec, doc in idx.items():
            alias = code_to_alias.get(ec)
            if alias is None:
                continue
            doc_id = doc["docID"]
            period_end = doc.get("periodEnd")
            source_url = f"{api_base}/documents/{doc_id}?type=5"
            try:
                zip_bytes = get_doc_zip_cached(api_base, doc_id, key, raw_dir)
                df = main_report_tsv(zip_bytes)
            except Exception as e:
                logger.error("取得/解凍失敗 alias=%s year=%s doc=%s: %s（スキップ）", alias, label, doc_id, e)
                continue
            if not period_end:
                period_end = period_end_from_df(df)
            if not period_end:
                logger.error("会計期末不明 alias=%s doc=%s（スキップ）", alias, doc_id)
                continue

            n_ok = 0
            for ind_id, (element_id, context_id, fb) in ind_maps[alias].items():
                try:
                    raw_yen, used_el = lookup_value_chain(df, element_id, context_id, fb)
                except Exception as e:
                    logger.error("値矛盾 %s @ %s (%s): %s（スキップ）", ind_id, period_end, doc_id, e)
                    missing.append(f"{ind_id}@{period_end}(conflict)")
                    continue
                if raw_yen is None:
                    missing.append(f"{ind_id}@{period_end}")
                    continue
                if used_el != element_id:
                    fallbacks_used.append(f"{ind_id}@{period_end}->{used_el}")
                    logger.info("fallback 採用: %s @ %s -> %s", ind_id, period_end, used_el)
                rows_by_ind[ind_id].append({
                    "date": period_end,
                    "indicator_id": ind_id,
                    "region": "JP",
                    "value": to_value_mn(raw_yen),
                    "source_url": source_url,
                })
                n_ok += 1
            coverage[alias].append(period_end)
            docs_processed += 1
            logger.info("alias=%s year=%s doc=%s 期末=%s 指標=%d/%d",
                        alias, label, doc_id, period_end, n_ok, len(ind_maps[alias]))

    if not rows_by_ind:
        msg = f"書き出す系列が 0 件（filer={aliases}, backfill_years={args.backfill_years}）"
        logger.error(msg)
        append_log(log_dir, "fetch_edinet", "FAIL", msg)
        return 1

    # --- 指標ごとに 1 ファイルへ書き出し（複数年度を 1 long CSV に） ---
    written: list[str] = []
    for ind_id in sorted(rows_by_ind):
        long_df = pd.DataFrame(rows_by_ind[ind_id])
        write_processed(long_df, processed_dir, basename=ind_id)
        write_metadata_for_indicator(processed_dir, source_cfg, ind_id, long_df)
        written.append(ind_id)

    # --- サマリ表示（桁の目視確認用） ---
    print("=" * 78)
    print(f"[EDINET Phase 2] filers={len(aliases)} docs={docs_processed} series_written={len(written)} 単位=百万円")
    for alias in aliases:
        ends = sorted(set(coverage.get(alias, [])))
        rng = f"{ends[0]}..{ends[-1]} ({len(ends)}期)" if ends else "なし"
        rev_id = f"edinet-{alias}-revenue"
        rev_latest = None
        if rows_by_ind.get(rev_id):
            latest = max(rows_by_ind[rev_id], key=lambda r: r["date"])
            rev_latest = (latest["date"], latest["value"])
        name = filers[alias].get("name_ja", alias)
        rev_str = f"  最新営業収益 {rev_latest[1]:>12,.0f}百万円 ({rev_latest[0]})" if rev_latest else ""
        print(f"  {alias:8s} {name:20s} 年度 {rng}{rev_str}")
    print("=" * 78)
    if fallbacks_used:
        print(f"fallback 採用 {len(fallbacks_used)} 件: {fallbacks_used[:10]}{' ...' if len(fallbacks_used)>10 else ''}")
    if missing:
        print(f"欠落（値なし）{len(missing)} 件（書き出しからは除外, 値は作らない）: {missing[:20]}{' ...' if len(missing)>20 else ''}")

    summary = (f"filers={len(aliases)} docs={docs_processed} series={len(written)} "
               f"missing={len(missing)} fallback={len(fallbacks_used)}")
    logger.info("done: %s", summary)
    append_log(log_dir, "fetch_edinet", "OK", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
