# -*- coding: utf-8 -*-
"""
把「操作建議」欄位補到既有的 stock_analysis_log
=================================================
claude_stock_analyzer v3.7 之後會自己產生這些欄位，但先前跑出來的紀錄
沒有。這支工具直接在既有檔案上補算，不需要重跑分析（所有需要的量
——股價、ATR、日波動度、model_quality——log 裡都已經有了）。

補上的欄位：
    操作建議        買不買，一句話
    建議買進價      現價 − 0.5×ATR（掛限價等回檔，不追高）
    建議停損價      買進價 − 1.5×ATR（跌破就走）
    建議目標價      買進價 ×(1 + 日波動度×√5)，持有約 5 個交易日
    Risk_Reward_Ratio  (目標−買進)/(買進−停損)
    建議理由        為什麼是這個建議

⚠ 「買不買」由 model_quality 決定，不由機率大小決定。當模型準確率的
   信賴區間下限沒超過 50%，再漂亮的機率也是雜訊，一律「不建議進場」。
   此時價位欄位仍會填，那是給你「若自行決定要進場」的風控參考，
   不是買進訊號。詳見 analyzer 裡 build_action_advice() 的說明。

使用方式：
    python add_action_column.py stock_analysis_log_v3.7.xlsx
    python add_action_column.py stock_analysis_log_v3.7.xlsx --print   # 順便印出來看
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


def load_analyzer():
    for base in (SCRIPT_DIR, SCRIPT_DIR.parent, SCRIPT_DIR / "tools"):
        p = base / "claude_stock_analyzer_v3.7.py"
        if p.exists():
            spec = importlib.util.spec_from_file_location("analyzer", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            print(f"[載入] {p.name}")
            return m
    print("✗ 找不到 claude_stock_analyzer_v3.7.py")
    print("  請把本工具放在與 analyzer 同一個資料夾（或其 tools/ 子目錄）。")
    sys.exit(1)


NEW_COLUMNS = ["操作建議", "建議買進價", "建議停損價", "建議目標價",
               "Risk_Reward_Ratio", "建議理由"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="補上操作建議欄位")
    ap.add_argument("log", type=Path)
    ap.add_argument("--print", dest="show", action="store_true", help="計算後印出結果")
    ap.add_argument("--no-write", action="store_true", help="只計算不寫檔")
    args = ap.parse_args(argv)

    if not args.log.exists():
        print(f"找不到檔案：{args.log}")
        return 1

    a = load_analyzer()
    df = pd.read_excel(args.log, sheet_name=SHEET)
    print(f"[讀取] {len(df)} 列")

    need = ["目前股價", "ATR", "日波動度std(%)", "model_quality",
            "Strategy_Decision", "綜合分數"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        print(f"✗ 紀錄檔缺少必要欄位：{missing}")
        print("  若欄位名稱看起來錯位，請先執行 repair_log.py 修復。")
        return 1

    rows = []
    for _, r in df.iterrows():
        skip = r.get("跳過原因")
        rows.append(a.build_action_advice(
            price=r.get("目前股價"),
            atr=r.get("ATR"),
            daily_std_pct=r.get("日波動度std(%)"),
            model_quality=r.get("model_quality"),
            strategy_decision=r.get("Strategy_Decision"),
            total_score=r.get("綜合分數"),
            skip_reason=None if pd.isna(skip) or not str(skip).strip() else str(skip),
        ))
    adv = pd.DataFrame(rows)
    for c in NEW_COLUMNS:
        df[c] = adv[c].values

    print(f"\n[結果] 操作建議分布：{df['操作建議'].value_counts().to_dict()}")

    if args.show:
        cols = ["股票代碼", "目前股價", "操作建議", "建議買進價",
                "建議停損價", "建議目標價", "Risk_Reward_Ratio"]
        cols = [c for c in cols if c in df.columns]
        print("\n" + df[cols].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    if args.no_write:
        print("\n（--no-write，未修改檔案）")
        return 0

    backup = args.log.with_name(args.log.stem + "_加建議前備份" + args.log.suffix)
    shutil.copy2(args.log, backup)
    with pd.ExcelWriter(args.log, engine="openpyxl") as w:
        df.to_excel(w, sheet_name=SHEET, index=False)
    print(f"\n✓ 已寫回：{args.log}（原檔備份：{backup.name}）")
    print("\n⚠ 提醒：價位以「建議買進價」為基準。若你已經持有，請改用自己的")
    print("   實際成本價套同一組倍數——停損的意義是相對你的成本虧多少。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
