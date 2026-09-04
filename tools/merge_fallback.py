# -*- coding: utf-8 -*-
"""
把 CSV 備援檔合併回主要的分析紀錄
==================================
claude_stock_analyzer 寫入 Excel 失敗時（最常見的原因是你正好把
stock_analysis_log_v3.7.xlsx 開在 Excel 裡，檔案被鎖住），會改存成
同名的 _fallback.csv，避免那次分析結果直接消失。

但那些紀錄只在 CSV 裡，不會出現在任何統計中——命中率、回填、模擬
全都看不到它們。這支工具負責把它們併回主 log。

它處理三件麻煩事：

1. 去重。CSV 裡的紀錄可能有一部分後來又成功寫進 Excel 了（例如你關掉
   Excel 之後重跑）。以「執行時間 + 股票代碼」為鍵比對，只補進主 log
   沒有的那些，不會產生重複列。

2. 欄位版本不一致。_append_csv_fallback 每次都用「當下的」欄位清單寫入，
   但檔案已存在時不會重寫表頭。所以升版之後繼續 append，CSV 也會出現
   跟 Excel 一樣的錯位問題。這裡沿用 repair_log 的逐列版本判斷，
   每一列各自對齊後才合併。

3. 合併後把 CSV 改名為 _已合併_YYYYMMDD.csv，避免下次重複合併。

使用方式：
    python merge_fallback.py                 # 自動尋找 CSV 與主 log
    python merge_fallback.py --dry-run       # 只檢查，不修改任何檔案
    python merge_fallback.py --csv A.csv --log B.xlsx
    python merge_fallback.py --keep-csv      # 合併後不改名 CSV

合併前會自動備份主 log。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import shutil
import sys
from pathlib import Path

import pandas as pd

SHEET = "分析紀錄"
SCRIPT_DIR = Path(__file__).resolve().parent
# 以「哪一次執行、分析哪一檔」當唯一鍵。同一檔在同一秒被分析兩次不可能發生，
# 所以這組鍵足以判斷是不是同一筆紀錄。
DEDUP_KEYS = ["執行時間", "股票代碼"]


def _load_repair_helpers():
    """沿用 repair_log 的欄位版本判斷邏輯，不重複實作一份。"""
    for base in (SCRIPT_DIR, SCRIPT_DIR.parent, SCRIPT_DIR / "tools"):
        p = base / "repair_log.py"
        if p.exists():
            spec = importlib.util.spec_from_file_location("repair_log", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    print("✗ 找不到 repair_log.py，無法判斷欄位版本。")
    print("  請把它放在與本工具同一個資料夾。")
    sys.exit(1)


def find_file(patterns: list[str], exclude: tuple[str, ...] = ()) -> Path | None:
    for pat in patterns:
        cands = [p for p in SCRIPT_DIR.glob(pat)
                 if not any(k in p.name for k in exclude)]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)
    return None


def read_csv_rows(path: Path, schema: list[str], helpers) -> pd.DataFrame:
    """
    逐列讀取 CSV 並各自判斷欄位版本後對齊，再回傳以目前 schema 為欄位的表。
    不信任 CSV 的表頭——它可能是舊版寫的，跟後面的資料列對不上。
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return pd.DataFrame(columns=schema)

    known = helpers.historical_schemas(schema)
    candidates = [known[k] for k in sorted(known, reverse=True)]
    by_version: dict[int, int] = {}
    fixed = []

    for raw in rows[1:]:                      # 第 1 列是表頭，不採用
        if not any(str(c).strip() for c in raw):
            continue
        best, best_score, best_row = None, None, None
        for cand in candidates:
            rr = raw[:len(cand)] if len(raw) > len(cand) else raw + [None] * (len(cand) - len(raw))
            sc = helpers._row_score(rr, cand)
            if best_score is None or sc > best_score:
                best, best_score, best_row = cand, sc, rr
        by_version[len(best)] = by_version.get(len(best), 0) + 1
        fixed.append(dict(zip(best, best_row)))

    if len(by_version) > 1:
        print(f"  註：CSV 裡混著多種欄位版本，已逐列分別對齊：")
        for w, n in sorted(by_version.items(), reverse=True):
            print(f"      {w} 欄 × {n} 列")

    df = pd.DataFrame(fixed).reindex(columns=schema)
    # CSV 全部是字串，數值欄位轉回數字，否則併進 Excel 之後無法計算
    for c in df.columns:
        if c in ("執行時間", "股票代碼", "股價日期(資料基準日)", "預測目標日(隔日估計)",
                 "完整終端輸出", "綜合結論", "操作建議", "建議理由"):
            continue
        converted = pd.to_numeric(df[c], errors="coerce")
        if converted.notna().sum() > 0:
            df[c] = df[c].where(converted.isna(), converted)
    return df


def make_key(df: pd.DataFrame) -> pd.Series:
    parts = []
    for k in DEDUP_KEYS:
        col = df[k] if k in df.columns else pd.Series([""] * len(df), index=df.index)
        parts.append(col.astype(str).str.strip())
    return parts[0].str.cat(parts[1:], sep="||")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 CSV 備援檔合併回主要分析紀錄")
    ap.add_argument("--csv", type=Path, default=None, help="備援 CSV 路徑")
    ap.add_argument("--log", type=Path, default=None, help="主要紀錄檔路徑")
    ap.add_argument("--dry-run", action="store_true", help="只檢查，不修改任何檔案")
    ap.add_argument("--keep-csv", action="store_true", help="合併後不要改名 CSV")
    args = ap.parse_args(argv)

    helpers = _load_repair_helpers()
    schema = helpers.load_schema()

    csv_path = args.csv or find_file(["*_fallback.csv"], exclude=("已合併",))
    if csv_path is None or not csv_path.exists():
        print("找不到備援 CSV（檔名形如 stock_analysis_log*_fallback.csv）。")
        print("沒有這個檔案是好事——代表每次分析都成功寫進 Excel。")
        return 0

    log_path = args.log or find_file(
        ["stock_analysis_log_v3.7.xlsx", "stock_analysis_log*.xlsx"],
        exclude=("備份", "old file", "fallback", "已修復", "修復前", "加建議前"))
    if log_path is None or not log_path.exists():
        print("找不到主要紀錄檔。請用 --log 指定。")
        return 1

    print(f"\n  備援 CSV：{csv_path.name}（{csv_path.stat().st_size / 1024:.0f} KB）")
    print(f"  主要紀錄：{log_path.name}")

    fb = read_csv_rows(csv_path, schema, helpers)
    main_df = pd.read_excel(log_path, sheet_name=SHEET)
    main_df = main_df.reindex(columns=schema)
    print(f"\n  CSV 內共 {len(fb)} 筆　主 log 內共 {len(main_df)} 筆")

    if fb.empty:
        print("  CSV 沒有可用資料，不需要合併。")
        return 0

    existing = set(make_key(main_df))
    fb_keys = make_key(fb)
    new_rows = fb[~fb_keys.isin(existing)].copy()
    dup_in_fb = new_rows.duplicated(subset=[k for k in DEDUP_KEYS if k in new_rows.columns])
    if dup_in_fb.any():
        print(f"  （CSV 內部有 {int(dup_in_fb.sum())} 筆重複，只保留一筆）")
        new_rows = new_rows[~dup_in_fb]

    print(f"  其中 {len(fb) - len(new_rows)} 筆主 log 已經有了，"
          f"{len(new_rows)} 筆是缺的")

    if new_rows.empty:
        print("\n✓ 沒有遺漏的紀錄，主 log 已經完整。")
        if not args.keep_csv and not args.dry_run:
            done = csv_path.with_name(
                csv_path.stem + f"_已合併_{dt.date.today().strftime('%Y%m%d')}.csv")
            csv_path.rename(done)
            print(f"  已把 CSV 改名為 {done.name}，之後不會再被掃到。")
        return 0

    show = [c for c in ["執行時間", "股票代碼", "股價日期(資料基準日)",
                        "Strategy_Decision", "model_quality"] if c in new_rows.columns]
    print(f"\n  要補進去的紀錄：")
    print(new_rows[show].head(20).to_string(index=False))
    if len(new_rows) > 20:
        print(f"    ...（另有 {len(new_rows) - 20} 筆）")
    if "股價日期(資料基準日)" in new_rows.columns:
        days = sorted(new_rows["股價日期(資料基準日)"].dropna().astype(str).unique())
        print(f"\n  涵蓋資料基準日：{days}")

    if args.dry_run:
        print("\n（--dry-run，未修改任何檔案）")
        return 0

    merged = pd.concat([main_df, new_rows], ignore_index=True)
    if "執行時間" in merged.columns:
        merged = merged.sort_values("執行時間", kind="stable").reset_index(drop=True)
    merged = merged.reindex(columns=schema)

    backup = log_path.with_name(log_path.stem + "_合併前備份" + log_path.suffix)
    shutil.copy2(log_path, backup)
    try:
        with pd.ExcelWriter(log_path, engine="openpyxl") as w:
            merged.to_excel(w, sheet_name=SHEET, index=False)
    except PermissionError:
        print(f"\n✗ 無法寫入 {log_path.name}——檔案正開在 Excel 裡，請先關閉再執行一次。")
        return 1

    print(f"\n✓ 已合併：{len(main_df)} + {len(new_rows)} = {len(merged)} 筆")
    print(f"  主 log：{log_path.name}")
    print(f"  合併前備份：{backup.name}")

    if not args.keep_csv:
        done = csv_path.with_name(
            csv_path.stem + f"_已合併_{dt.date.today().strftime('%Y%m%d')}.csv")
        csv_path.rename(done)
        print(f"  CSV 已改名為 {done.name}（避免下次重複合併）")

    print(f"\n  接著建議執行：")
    print(f"    python stock.py repair    # 確認欄位結構正確")
    print(f"    python stock.py review    # 重新回填實際結果與命中率")
    print(f"\n  ⚠ 以後跑分析之前，記得先把 Excel 檔關掉，就不會再產生備援 CSV。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
