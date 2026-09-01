# -*- coding: utf-8 -*-
"""
stock_analysis_log 表頭錯位修復工具
====================================
症狀：用 Excel 打開紀錄檔，發現「程式版本」欄位裡是數字、「實際報酬(%)」
      欄位裡是 3.7 這種明顯放錯位置的值；或用 log_review.py 跑出來的
      統計完全不合理。

成因：紀錄檔的表頭是舊版 analyzer 寫的（欄位較少），但資料列是新版寫的
      （欄位較多）。openpyxl 的 append 會把新版的值一路寫到底，多出來的
      那幾欄沒有表頭，於是從差異點開始，每一欄的值都對到了錯誤的名稱上。
      這種錯位不會噴任何錯誤，但會讓後續所有統計靜靜地算在錯的欄位上。

修復：本工具**不看表頭**，直接讀取資料列，再依目前 analyzer 的正確欄位
      清單重新命名。同時可選擇移除重複紀錄。原檔會先備份。

使用方式：
    python repair_log.py stock_analysis_log_v3.7.xlsx           # 檢查並修復
    python repair_log.py stock_analysis_log_v3.7.xlsx --check   # 只檢查不改
    python repair_log.py stock_analysis_log_v3.7.xlsx --dedup   # 順便移除重複列
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

import pandas as pd

SHEET = "分析紀錄"
SCRIPT_DIR = Path(__file__).resolve().parent


def find_analyzer() -> Path | None:
    for name in ("claude_stock_analyzer_v3.7.py", "claude_stock_analyzer_v3.6.py"):
        for base in (SCRIPT_DIR, SCRIPT_DIR.parent, SCRIPT_DIR / "tools"):
            p = base / name
            if p.exists():
                return p
    return None


def load_schema() -> list[str]:
    p = find_analyzer()
    if p is None:
        print("✗ 找不到 claude_stock_analyzer_v3.7.py，無法取得正確的欄位清單。")
        print("  請把本工具放在與 analyzer 同一個資料夾（或其 tools/ 子目錄）。")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("analyzer", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    print(f"[結構] 取自 {p.name}：{len(m.EXCEL_LOG_COLUMNS)} 個欄位")
    return list(m.EXCEL_LOG_COLUMNS)


def inspect(path: Path, schema: list[str]) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb[SHEET] if SHEET in wb.sheetnames else wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return {"ok": False, "reason": "檔案是空的"}

    header = list(rows[0])
    data = [list(r) for r in rows[1:]]
    # 去掉表頭尾端的空白欄位再比較，避免 Excel 存檔留下的空欄造成誤判
    trimmed = list(header)
    while trimmed and trimmed[-1] in (None, ""):
        trimmed.pop()

    widths = {len(r) for r in data} if data else set()
    return {
        "ok": True,
        "header": header,
        "trimmed_header": trimmed,
        "data": data,
        "n_rows": len(data),
        "header_width": len(trimmed),
        "data_widths": widths,
        "schema_width": len(schema),
        "aligned": trimmed == schema,
    }


def diagnose(info: dict, schema: list[str]) -> bool:
    """回傳 True 代表需要修復。"""
    print(f"\n[檢查] 資料列數：{info['n_rows']}")
    print(f"       表頭欄位數（去除尾端空白）：{info['header_width']}")
    print(f"       資料列欄位數：{sorted(info['data_widths'])}")
    print(f"       正確欄位數：{info['schema_width']}")

    if info["aligned"] and info["data_widths"] <= {info["schema_width"]}:
        print("\n✓ 表頭與資料一致，不需要修復。")
        return False

    print("\n✗ 偵測到錯位：")
    if not info["aligned"]:
        for i, (h, s) in enumerate(zip(info["trimmed_header"], schema)):
            if h != s:
                print(f"    第 {i + 1} 欄開始不一致：")
                print(f"      檔案表頭：{h}")
                print(f"      應該是：  {s}")
                break
        else:
            print(f"    表頭長度 {info['header_width']} ≠ 正確長度 {info['schema_width']}")
    if info["data_widths"] - {info["schema_width"]}:
        print(f"    有資料列的欄位數不等於 {info['schema_width']}：{sorted(info['data_widths'])}")
    print("\n  影響：從不一致的那一欄開始，之後每一欄的值都對到錯誤的名稱上。")
    print("  這不會噴錯，但 log_review.py 等工具會靜靜地拿錯欄位做統計。")
    return True


# 歷史欄位結構。新版若在「中間」插入欄位，舊資料就不能靠尾端補空來對齊——
# 那樣會讓插入點之後的每一欄再次錯位（本工具原本就踩過這個坑）。這裡用
# 「欄位寬度 → 當時的欄位清單」反推舊資料的真實結構，再依欄位名稱對映到
# 目前的結構，缺的補空、多的丟棄。
#
# 由目前結構往回推導，不需手動維護整份清單：
#   106 欄：目前（新增 操作建議／建議買進價／建議理由）
#   103 欄：新增 較同批次落後天數／是否較同批次落後 之後
#   101 欄：v3.7 初版
COLUMNS_ADDED_LATER = [
    ["操作建議", "建議買進價", "建議理由"],          # 106 → 103
    ["較同批次落後天數", "是否較同批次落後"],          # 103 → 101
]


def historical_schemas(current: list[str]) -> dict:
    """回傳 {欄位寬度: 該版本的欄位清單}，含目前版本與所有已知舊版。"""
    out = {len(current): list(current)}
    cols = list(current)
    for removed in COLUMNS_ADDED_LATER:
        cols = [c for c in cols if c not in removed]
        out.setdefault(len(cols), list(cols))
    return out


def repair(path: Path, info: dict, schema: list[str], dedup: bool) -> None:
    w = len(schema)
    known = historical_schemas(schema)
    dropped = 0

    widths = sorted(info["data_widths"])
    data_width = widths[-1] if widths else w
    source_schema = known.get(data_width)
    if source_schema is None:
        print(f"\n  ⚠ 資料寬度 {data_width} 不符合任何已知版本"
              f"（已知：{sorted(known)}），改用尾端截斷/補空處理。")
        print(f"     若修復後仍有欄位看起來錯位，請把檔案傳出來判斷。")
        source_schema = schema
    elif data_width != w:
        missing = [c for c in schema if c not in source_schema]
        print(f"\n  資料是 {data_width} 欄版本寫的，目前結構是 {w} 欄。")
        print(f"  依欄位名稱對映，新增的欄位留空：{missing}")

    fixed = []
    for r in info["data"]:
        r = list(r)
        if len(r) > len(source_schema):
            r = r[:len(source_schema)]
        elif len(r) < len(source_schema):
            r = r + [None] * (len(source_schema) - len(r))
        fixed.append(r)

    # 先用「資料當時的結構」建表，再依欄位名稱對映到目前結構。
    # 這一步是關鍵：中間插入的新欄位會被放到正確位置並留空，
    # 而不是把舊資料整段往後推。
    df = pd.DataFrame(fixed, columns=source_schema).reindex(columns=schema)

    if dedup:
        before = len(df)
        # 同一檔、同一資料基準日只保留最後一筆（與 log_review 的去重規則一致）
        if {"股票代碼", "股價日期(資料基準日)", "執行時間"} <= set(df.columns):
            df = (df.sort_values("執行時間")
                    .groupby(["股票代碼", "股價日期(資料基準日)"], as_index=False)
                    .last())
            df = df[schema]
        dropped = before - len(df)

    backup = path.with_name(path.stem + "_修復前備份" + path.suffix)
    shutil.copy2(path, backup)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=SHEET, index=False)

    print(f"\n✓ 已修復：{path}")
    print(f"  原檔備份：{backup.name}")
    print(f"  資料列數：{len(df)}" + (f"（移除 {dropped} 筆重複）" if dropped else ""))

    # 修復後的抽樣驗證
    print("\n[驗證] 隨機抽查幾個容易看出錯位的欄位：")
    for col in ["程式版本", "綜合結論", "三大法人連續方向", "model_quality"]:
        if col in df.columns:
            vals = df[col].dropna().unique()[:3]
            print(f"    {col}: {list(vals)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="stock_analysis_log 表頭錯位修復")
    ap.add_argument("log", type=Path)
    ap.add_argument("--check", action="store_true", help="只檢查，不修改檔案")
    ap.add_argument("--dedup", action="store_true",
                    help="同時移除重複紀錄（同一檔、同一資料基準日只留最後一筆）")
    args = ap.parse_args(argv)

    if not args.log.exists():
        print(f"找不到檔案：{args.log}")
        return 1

    schema = load_schema()
    info = inspect(args.log, schema)
    if not info["ok"]:
        print(f"✗ {info['reason']}")
        return 1

    need = diagnose(info, schema)
    if not need and not args.dedup:
        return 0
    if args.check:
        print("\n（--check 模式，未修改檔案）")
        return 0

    repair(args.log, info, schema, args.dedup)
    print("\n修復後請重新執行：python log_review.py <紀錄檔> --write-back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
