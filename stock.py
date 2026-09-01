# -*- coding: utf-8 -*-
"""
台股分析系統 統一入口 v1.0
============================
一支程式取代原本 10 個要記的進入點。它本身不做任何分析，只負責把
指令轉給對應的工具——各工具仍然可以單獨執行，行為完全不變。

    python stock.py daily      每個交易日收盤後跑這個（最常用）
    python stock.py report     看模擬績效
    python stock.py status     看目前現金與持股
    python stock.py review     回填實際結果、更新命中率統計
    python stock.py advice     把操作建議欄位補到既有紀錄檔
    python stock.py compare    比較活躍股預測 v2.1 / v3.0 / v3.1
    python stock.py settle     結算出清模擬部位（想收尾時才用）
    python stock.py repair     修復欄位錯位的紀錄檔
    python stock.py check      環境自檢：套件、檔案、版本、設定
    python stock.py archive    把舊版程式移到 舊版/ 資料夾

不帶參數執行會列出所有指令與目前狀態，所以用 VS Code 的 ▶ Run 按鈕
也看得到東西，不會只噴一行看不懂的錯誤。

設定集中在 stock_settings.json，改那一個檔案就好。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / "stock_settings.json"

# 目前使用的版本。archive 指令會把不在這裡的舊版本移走。
CURRENT = {
    "活躍股篩選": ["tw_active_stocks_predictor_v3.1.py"],
    "個股分析": ["claude_stock_analyzer_v3.7.py"],
    "整合流程": ["tw_stock_pipeline_v1.1.py"],
    "工具": ["paper_trading.py", "log_review.py", "compare_predictors.py",
             "repair_log.py", "add_action_column.py"],
}

OBSOLETE_PATTERNS = [
    "tw_active_stocks_predictor_v1*.py", "tw_active_stocks_predictor_v2*.py",
    "tw_active_stocks_predictor_v3.0.py",
    "claude_stock_analyzer_v3.py", "claude_stock_analyzer_v3.[0-6].py",
    "tw_stock_pipeline_v1.0.py",
]

REQUIRED_PACKAGES = ["yfinance", "pandas", "numpy", "requests", "openpyxl", "sklearn"]


def find(name: str) -> Path | None:
    """在同目錄、tools/、上層依序找檔案。"""
    for base in (ROOT, ROOT / "tools", ROOT.parent, ROOT.parent / "tools"):
        p = base / name
        if p.exists():
            return p
    return None


def find_log() -> Path | None:
    """找出目前在用的紀錄檔，優先取最近修改的那一個。"""
    cands = sorted(ROOT.glob("stock_analysis_log*.xlsx"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    cands = [p for p in cands if "備份" not in p.name]
    return cands[0] if cands else None


def run(script: str, args: list[str]) -> int:
    p = find(script)
    if p is None:
        print(f"✗ 找不到 {script}")
        print(f"  請確認它在 {ROOT} 或 {ROOT / 'tools'} 底下。")
        return 1
    cmd = [sys.executable, str(p)] + args
    print(f"→ {' '.join(str(c) for c in cmd)}\n")
    return subprocess.run(cmd, cwd=ROOT).returncode


# ============================================================
# 原生指令
# ============================================================

def cmd_check() -> int:
    """環境自檢。混版執行曾經害紀錄檔欄位錯位，所以這一項值得每次升版後跑。"""
    print("=" * 66)
    print("  環境自檢")
    print("=" * 66)
    ok = True

    print(f"\n[1/5] Python：{sys.version.split()[0]}  ({sys.executable})")

    print("\n[2/5] 套件")
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg)
            print(f"    ✓ {pkg}")
        except ImportError:
            ok = False
            print(f"    ✗ {pkg}　← 執行：python -m pip install "
                  + ("scikit-learn" if pkg == "sklearn" else pkg))

    print("\n[3/5] 程式檔案")
    for group, names in CURRENT.items():
        for n in names:
            p = find(n)
            if p:
                try:
                    where = p.relative_to(ROOT)
                except ValueError:
                    where = p
                print(f"    ✓ {group:<8} {where}")
            else:
                ok = False
                print(f"    ✗ {group:<8} {n}　← 缺少")

    print("\n[4/5] 舊版程式（建議用 archive 移走，避免混版）")
    stale = []
    for pat in OBSOLETE_PATTERNS:
        stale += [p for p in ROOT.glob(pat) if p.is_file()]
    stale = sorted(set(stale))
    if stale:
        for p in stale:
            print(f"    ⚠ {p.name}")
        print(f"    → 執行 python stock.py archive 可一次移到 舊版/ 資料夾")
    else:
        print("    ✓ 沒有殘留的舊版檔案")

    print("\n[5/5] 設定與資料")
    if SETTINGS_FILE.exists():
        try:
            cfg = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            fee = cfg.get("券商與稅費", {})
            d = float(fee.get("fee_discount", 0.6))
            tax = float(fee.get("securities_tax_pct", 0.3))
            print(f"    ✓ stock_settings.json：手續費折扣 {d}、證交稅 {tax}%")
            print(f"      → 來回成本約 {0.1425 * d * 2 + tax:.3f}%")
        except Exception as e:
            ok = False
            print(f"    ✗ stock_settings.json 格式錯誤：{type(e).__name__}: {e}")
    else:
        print("    ⚠ 找不到 stock_settings.json，各程式將使用內建預設值")

    log = find_log()
    if log:
        try:
            import pandas as pd
            df = pd.read_excel(log, sheet_name="分析紀錄")
            print(f"    ✓ 紀錄檔 {log.name}：{len(df)} 列 × {len(df.columns)} 欄")
            an = find("claude_stock_analyzer_v3.7.py")
            if an:
                import importlib.util
                spec = importlib.util.spec_from_file_location("an", an)
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)
                want = len(m.EXCEL_LOG_COLUMNS)
                if len(df.columns) != want:
                    ok = False
                    print(f"      ✗ 欄位數應為 {want}，實際 {len(df.columns)}"
                          f" → 執行 python stock.py repair")
                elif list(df.columns) != m.EXCEL_LOG_COLUMNS:
                    ok = False
                    print(f"      ✗ 欄位名稱或順序不符 → 執行 python stock.py repair")
                else:
                    print(f"      ✓ 欄位結構正確")
            if "股價日期(資料基準日)" in df.columns:
                days = sorted(df["股價日期(資料基準日)"].astype(str).unique())
                print(f"      涵蓋 {len(days)} 個交易日：{days[0]} ~ {days[-1]}")
        except Exception as e:
            print(f"    ⚠ 紀錄檔讀取失敗：{type(e).__name__}: {e}")
    else:
        print("    · 尚未產生紀錄檔（第一次執行 daily 之後才會有）")

    state = ROOT / "paper_trading_state.json"
    print(f"    {'✓' if state.exists() else '·'} 模擬狀態："
          + (f"{state.name} 已建立" if state.exists()
             else "尚未建立 → python stock.py sim-init"))

    print("\n" + "=" * 66)
    print("  自檢結果：" + ("全部正常" if ok else "有項目需要處理，見上方 ✗"))
    print("=" * 66)
    return 0 if ok else 1


def cmd_archive(dry: bool) -> int:
    """把舊版程式移到 舊版/ 資料夾。"""
    stale = []
    for pat in OBSOLETE_PATTERNS:
        stale += [p for p in ROOT.glob(pat) if p.is_file()]
    stale = sorted(set(stale))
    if not stale:
        print("沒有需要歸檔的舊版檔案。")
        return 0

    dest = ROOT / "舊版"
    print(f"以下 {len(stale)} 個舊版檔案將移到 {dest.name}/：")
    for p in stale:
        print(f"    {p.name}")
    print("\n為什麼要移走：混版執行曾經造成紀錄檔欄位錯位——表頭是舊版的、")
    print("資料是新版的，從第 13 欄開始每一欄的值都對到錯誤的名稱，而且")
    print("不會噴任何錯誤。把舊版移出主資料夾可以從源頭避免這件事。")

    if dry:
        print("\n（--dry-run，未實際移動）")
        return 0

    dest.mkdir(exist_ok=True)
    for p in stale:
        shutil.move(str(p), str(dest / p.name))
    print(f"\n✓ 已移動 {len(stale)} 個檔案到 {dest}")
    print("  舊版仍然保留著，需要時可以自己搬回來。")
    return 0


def cmd_overview() -> int:
    """不帶參數時顯示：目前狀態 + 可用指令。"""
    today = dt.date.today()
    print("=" * 66)
    print("  台股分析系統")
    print(f"  今天 {today}（{'一二三四五六日'[today.weekday()]}）"
          + ("　★ 週五：daily 會另存週報" if today.weekday() == 4 else ""))
    print("=" * 66)

    log = find_log()
    state = ROOT / "paper_trading_state.json"
    print(f"\n  紀錄檔：{log.name if log else '尚未建立'}")
    print(f"  模擬：  {'進行中' if state.exists() else '尚未建立'}")

    print("\n  最常用")
    print("    python stock.py daily      每個交易日收盤後跑（15:00 之後）")
    print("    python stock.py report     看模擬績效")
    print("    python stock.py check      環境自檢")
    print("\n  其他")
    print("    status   看目前現金與持股")
    print("    review   回填實際結果、更新命中率")
    print("    advice   把操作建議補到既有紀錄檔")
    print("    compare  比較活躍股預測各版本")
    print("    settle   結算出清模擬部位")
    print("    repair   修復欄位錯位的紀錄檔")
    print("    archive  把舊版程式移到 舊版/")
    print("    sim-init 重新建立一輪模擬")
    print("\n  設定集中在 stock_settings.json，改那一個檔案就好。")
    print("=" * 66)
    return 0


# ============================================================
# 主流程
# ============================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="台股分析系統統一入口", add_help=True)
    ap.add_argument("command", nargs="?", default=None, help="要執行的指令")
    ap.add_argument("extra", nargs="*", help="轉給下層工具的額外參數")
    ap.add_argument("--dry-run", action="store_true", help="archive 專用：只顯示不移動")
    args = ap.parse_args(argv)

    cmd = args.command
    extra = list(args.extra)

    if cmd is None:
        return cmd_overview()
    if cmd == "check":
        return cmd_check()
    if cmd == "archive":
        return cmd_archive(args.dry_run)

    if cmd == "daily":
        return run("tw_stock_pipeline_v1.1.py", extra)
    if cmd == "settle":
        return run("tw_stock_pipeline_v1.1.py", ["--settle"] + extra)

    if cmd in ("report", "status"):
        return run("paper_trading.py", [cmd] + extra)
    if cmd == "sim-init":
        cfg = {}
        if SETTINGS_FILE.exists():
            try:
                cfg = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("紙上交易模擬", {})
            except Exception:
                pass
        a = ["init", "--capital", str(cfg.get("capital", 1000000)),
             "--top-n", str(cfg.get("top_n", 5)),
             "--max-position-pct", str(cfg.get("max_position_pct", 0.25)),
             "--stop-loss-pct", str(cfg.get("stop_loss_pct", 0.07)),
             "--stop-loss-atr", str(cfg.get("stop_loss_atr", 2.0)),
             "--max-holding-days", str(cfg.get("max_holding_days", 10))]
        if not cfg.get("allow_odd_lot", True):
            a.append("--lot-only")
        return run("paper_trading.py", a + extra)

    log = find_log()
    if cmd in ("review", "repair", "advice") and log is None:
        print("✗ 找不到 stock_analysis_log*.xlsx，請先執行 python stock.py daily")
        return 1

    if cmd == "review":
        return run("log_review.py", [str(log), "--write-back"] + extra)
    if cmd == "repair":
        return run("repair_log.py", [str(log), "--dedup"] + extra)
    if cmd == "advice":
        return run("add_action_column.py", [str(log), "--print"] + extra)
    if cmd == "compare":
        return run("compare_predictors.py", extra)

    print(f"✗ 不認得的指令：{cmd}")
    return cmd_overview() or 1


if __name__ == "__main__":
    sys.exit(main())
