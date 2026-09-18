# -*- coding: utf-8 -*-
"""
獲利能力評估：四道關卡
========================
回答一個問題：**這套系統該不該拿真錢下去，以及該改什麼。**

判斷標準是在看到資料**之前**就寫死的（見下方常數）。這一點很重要——
如果先看數字再定標準，就會變成「找一個能支持這個數字的說法」。那是
curve fitting 的心理版本，比程式裡的過擬合更難察覺。

    Gate 1  模型有沒有 edge          Wilson 下界中位數 > 50%
    Gate 2  有沒有贏過什麼都不做      勝過 cash 且勝過 active_equal
    Gate 3  成本吃掉多少             每筆平均毛利 > 來回成本 × 1.5
    Gate 4  執行品質                 不需要 edge 就能改善的漏水

前三關講的是「有沒有賺錢的本事」，第四關講的是「有沒有把賺到的漏掉」。
**第四關最可能有實際收穫**，因為它修的是漏水，不是預測。

用法：
    python stock.py evaluate
    python stock.py evaluate --log 路徑.xlsx --state 路徑.json

沒有 state.json 時只跑 Gate 1 與 Gate 4，不會整支失敗。
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import importlib.util
import json
import math
import sys
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------------
# 先寫死的判斷標準（看資料前就固定，不因結果調整）
# ----------------------------------------------------------------------
GATE1_MIN_LOWER_BOUND = 50.0      # Wilson 下界要超過丟銅板
GATE3_COST_SAFETY = 1.5           # 毛利要有 1.5 倍成本的安全邊際
MIN_TRADES_FOR_CONCLUSION = 30    # 低於此數，任何結論都只是雜訊
CONFIDENCE_Z = 1.96               # 95% 信賴區間


# 紀錄檔的工作表名稱。其他工具（repair_log / log_review / merge_fallback）
# 都明確指名這一張；這裡也指名，不要靠「預設讀第一張」——log_review 會在
# 同一個活頁簿裡寫入命中率、機率校準等額外工作表，哪天順序變了，
# 預設讀法會安靜地讀到錯的表。
SHEET = "分析紀錄"


def read_log(path):
    """優先讀「分析紀錄」工作表；沒有那張表時退回預設（例如手工整理的檔）。"""
    try:
        return pd.read_excel(path, sheet_name=SHEET)
    except ValueError:
        return pd.read_excel(path)

# ----------------------------------------------------------------------
# 共用工具
# ----------------------------------------------------------------------

def find_file(name: str) -> Path | None:
    """
    在「自己所在層 → 上一層 → 各自的 tools\」依序找檔案。

    不要寫死 ROOT/"tools"/<name>：實測上 paper_trading.py 很可能被放在
    主資料夾而不是 tools\，寫死路徑就會在那台機器上直接找不到。
    這跟 stock.py 的 find() 是同一個策略。
    """
    here = Path(__file__).resolve().parent
    for base in (here, here / "tools", here.parent, here.parent / "tools",
                 Path.cwd(), Path.cwd() / "tools"):
        cand = base / name
        if cand.exists():
            return cand
    return None


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _borrow_function(src: Path, func_name: str):
    """
    從分析程式裡「只」取出一個函式，不 import 整個模組。

    claude_stock_analyzer 在 module 層就 import yfinance/sklearn，為了一個
    五行的公式把那些套件全載進來既慢又脆弱。但也不該自己複製一份——
    複製出來的版本會跟本尊走鐘，而走鐘時不會有任何錯誤訊息。
    所以用 ast 挑出那一個函式，單獨編譯。
    """
    try:
        tree = ast.parse(src.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            mod = ast.Module(body=[node], type_ignores=[])
            ns: dict = {"math": math, "np": np}
            try:
                exec(compile(mod, str(src), "exec"), ns)          # noqa: S102
            except Exception:                                      # noqa: BLE001
                return None
            return ns.get(func_name)
    return None


def _wilson_fallback(accuracy_pct, sample_size, z=1.96):
    if not sample_size or sample_size <= 0 or accuracy_pct is None:
        return None
    p = float(accuracy_pct) / 100.0
    n = float(sample_size)
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom * 100.0


def get_wilson():
    """優先用分析程式裡那一份，取不到才用本地備援。"""
    cands = sorted(ROOT.glob("claude_stock_analyzer_v*.py"))
    for p in reversed(cands):
        f = _borrow_function(p, "wilson_lower_bound")
        if f is not None:
            return f, p.name
    return _wilson_fallback, "(本地備援)"


def mean_ci(values, z=CONFIDENCE_Z):
    """回傳 (平均, 下界, 上界, n)。樣本不足 2 筆時界限為 nan。"""
    v = pd.Series(values, dtype="float64").dropna()
    n = len(v)
    if n == 0:
        return float("nan"), float("nan"), float("nan"), 0
    m = float(v.mean())
    if n < 2:
        return m, float("nan"), float("nan"), n
    se = float(v.std(ddof=1)) / math.sqrt(n)
    return m, m - z * se, m + z * se, n


def verdict(passed: bool | None) -> str:
    if passed is None:
        return "無法判斷"
    return "通過" if passed else "不通過"


def _hr(title=""):
    print("\n" + "=" * 70)
    if title:
        print(f"  {title}")
        print("=" * 70)


# ----------------------------------------------------------------------
# Gate 1：模型有沒有 edge
# ----------------------------------------------------------------------

LB_COLS = {"邏輯迴歸": "隔日_邏輯迴歸_準確率信賴下限(%)",
           "RF": "隔日_RF_準確率信賴下限(%)"}
ACC_COLS = {"邏輯迴歸": "隔日_邏輯迴歸_樣本外準確率(%)",
            "RF": "隔日_RF_樣本外準確率(%)"}
N_COLS = {"邏輯迴歸": "隔日_邏輯迴歸_樣本數", "RF": "隔日_RF_樣本數"}


def gate1_edge(df: pd.DataFrame) -> bool | None:
    _hr("Gate 1　模型有沒有 edge")
    print(f"  標準：Wilson 準確率下界中位數 > {GATE1_MIN_LOWER_BOUND:.0f}%")
    print("  意義：下界沒過 50%，代表無法排除「它其實在丟銅板」。\n")

    any_data = False
    results = {}
    for label, col in LB_COLS.items():
        if col not in df.columns:
            print(f"  {label}：紀錄檔沒有「{col}」欄位，無法判斷")
            continue
        lb = pd.to_numeric(df[col], errors="coerce").dropna()
        if lb.empty:
            print(f"  {label}：欄位存在但沒有數值")
            continue
        any_data = True
        acc = pd.to_numeric(df.get(ACC_COLS[label]), errors="coerce").dropna()
        n = pd.to_numeric(df.get(N_COLS[label]), errors="coerce").dropna()
        med = float(lb.median())
        results[label] = med
        print(f"  {label}")
        print(f"    準確率中位數      {acc.median():.1f}%" if len(acc)
              else "    準確率中位數      （無資料）")
        print(f"    Wilson 下界中位數 {med:.1f}%   ← 判斷用這個")
        print(f"    下界超過 50% 的比例 {(lb > 50).mean() * 100:.0f}%"
              f"（{int((lb > 50).sum())}/{len(lb)} 檔次）")
        if len(n):
            print(f"    walk-forward 樣本數中位數 {n.median():.0f}")
        print()

    if not any_data:
        print("  → 無法判斷：紀錄檔裡沒有信賴下限資料。")
        return None

    best = max(results.values())
    passed = best > GATE1_MIN_LOWER_BOUND
    print(f"  結果：{verdict(passed)}（最佳模型下界 {best:.1f}%）")
    if not passed:
        print("  → 模型不該驅動進場決策。任何正報酬只能歸因於運氣或市場本身，")
        print("    不能歸因於模型。這一關不過，就不要去調模型參數——")
        print("    那是對雜訊做曲線擬合，會讓回測變漂亮、實盤變差。")
    return passed


# ----------------------------------------------------------------------
# Gate 2：有沒有贏過什麼都不做
# ----------------------------------------------------------------------

BENCHMARKS = ("cash", "active_equal")
MODEL_DRIVEN = ("strategy_decision", "ev_decision")


def gate2_benchmark(sim) -> bool | None:
    _hr("Gate 2　有沒有贏過「什麼都不做」")
    print("  標準：模型驅動策略要同時贏過 cash（完全不交易）與")
    print("        active_equal（活躍股平均買，不用模型）")
    print("  關鍵：若 active_equal 贏過模型驅動策略 → 模型沒有貢獻，")
    print("        賺的是市場本身，不是選股能力。\n")

    s = sim.summary()
    show = ["策略", "報酬率(%)", "買進次數", "賣出次數", "勝率(%)",
            "累計交易成本", "最大回落(%)"]
    have = [c for c in show if c in s.columns]
    print(s[have].to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    ret = dict(zip(s["策略"], s["報酬率(%)"]))
    missing = [b for b in BENCHMARKS + MODEL_DRIVEN if b not in ret]
    if missing:
        print(f"\n  ⚠ 缺少策略：{', '.join(missing)}，無法完整比較")

    # 一筆都沒買，不是「持平」，是「進場條件從未被滿足」。
    # 那要診斷的不是模型準不準，而是門檻是否過嚴——或者它其實是對的。
    if "買進次數" in s.columns:
        idle = [r["策略"] for _, r in s.iterrows()
                if r["策略"] in MODEL_DRIVEN and r["買進次數"] == 0]
        if idle:
            print(f"\n  ⚠ {'、'.join(idle)} 整個期間一筆都沒買。")
            print("    報酬率 0% 不代表「持平」，是進場條件從未被滿足。")
            print("    這無法證明策略好或壞——它根本沒有被測試到。")

    # 用每日權益變化算報酬差的信賴區間。只比點估計會嚴重高估確定性：
    # 兩週半的資料，區間通常寬到足以涵蓋正負號相反的結論。
    curves = sim.all_curves()
    print("\n  與基準的每日報酬差（95% 信賴區間）")
    print("  " + "-" * 64)
    passed_any = False
    if curves.empty or "策略" not in curves.columns:
        print("  （沒有權益曲線，無法計算）")
    else:
        piv = curves.pivot(index="date", columns="策略", values="equity").sort_index()
        rets = piv.pct_change().dropna(how="all") * 100
        for strat in MODEL_DRIVEN:
            if strat not in rets.columns:
                continue
            beats = []
            for bm in BENCHMARKS:
                if bm not in rets.columns:
                    continue
                diff = (rets[strat] - rets[bm]).dropna()
                m, lo, hi, n = mean_ci(diff)
                sig = "顯著勝出" if lo > 0 else ("顯著落後" if hi < 0 else "分不出差異")
                beats.append(lo > 0)
                print(f"  {strat:18s} vs {bm:14s} "
                      f"每日 {m:+.3f}%  [{lo:+.3f}%, {hi:+.3f}%]  n={n}  {sig}")
            if beats and all(beats):
                passed_any = True

    n_trades = int(s["賣出次數"].max()) if "賣出次數" in s.columns else 0
    print()
    if n_trades < MIN_TRADES_FOR_CONCLUSION:
        print(f"  ⚠ 已平倉交易最多只有 {n_trades} 筆（需要 {MIN_TRADES_FOR_CONCLUSION} 筆）。")
        print("    這個樣本數不管結果正負都在雜訊範圍內，不足以下結論。")
        print(f"  結果：{verdict(None)}")
        return None

    print(f"  結果：{verdict(passed_any)}")
    if not passed_any:
        print("  → 沒有證據顯示模型驅動策略優於不用模型的做法。")
    return passed_any


# ----------------------------------------------------------------------
# Gate 3：成本吃掉多少
# ----------------------------------------------------------------------

def gate3_cost(sim, trades: pd.DataFrame) -> bool | None:
    _hr("Gate 3　成本吃掉多少")
    rt = sim_round_trip_cost(sim)
    need = rt * GATE3_COST_SAFETY
    print(f"  來回成本 {rt:.3f}%。標準：每筆平均毛利 > {need:.3f}%"
          f"（{GATE3_COST_SAFETY} 倍安全邊際，不是打平就算過）\n")

    if trades.empty or "報酬率(%)" not in trades.columns:
        print("  （沒有已平倉交易，無法判斷）")
        return None

    sells = trades[trades["side"] == "賣出"].copy()
    sells["報酬率(%)"] = pd.to_numeric(sells["報酬率(%)"], errors="coerce")
    if sells.empty:
        print("  （沒有賣出紀錄，無法判斷）")
        return None

    passed_any = False
    max_n = 0
    print(f"  {'策略':20s}{'筆數':>6s}{'平均毛利':>12s}{'95% 信賴區間':>24s}{'扣成本後':>12s}")
    print("  " + "-" * 74)
    for strat, g in sells.groupby("策略"):
        m, lo, hi, n = mean_ci(g["報酬率(%)"])
        max_n = max(max_n, n)
        net = m - rt
        mark = ""
        if n >= MIN_TRADES_FOR_CONCLUSION and lo > need:
            passed_any = True
            mark = "  ✓"
        print(f"  {strat:20s}{n:>6d}{m:>11.2f}%"
              f"{f'[{lo:+.2f}%, {hi:+.2f}%]':>24s}{net:>11.2f}%{mark}")

    total_cost = sim.summary()["成本佔初始資金(%)"].max()
    print(f"\n  期間累計交易成本佔初始資金最高 {total_cost:.2f}%")
    days = _sim_days(sim)
    if days > 0:
        print(f"  模擬 {days} 個交易日 → 年化成本約 {total_cost * 244 / days:.1f}%")
        print("  （這是模型必須先贏過的門檻，賺的錢要先填完這個洞）")

    enough = max_n >= MIN_TRADES_FOR_CONCLUSION
    if not enough:
        print(f"\n  ⚠ 最多只有 {max_n} 筆已平倉交易（需要 {MIN_TRADES_FOR_CONCLUSION} 筆）")
    print(f"  結果：{verdict(passed_any if enough else None)}")
    return passed_any if enough else None


def sim_round_trip_cost(sim) -> float:
    try:
        import paper_trading as pt
        return pt.round_trip_cost_pct(sim.cfg["fee_discount"])
    except Exception:                                              # noqa: BLE001
        return 0.471


def _sim_days(sim) -> int:
    for p in sim.portfolios.values():
        if p.equity_curve:
            return len(p.equity_curve)
    return 0


# ----------------------------------------------------------------------
# Gate 4：執行品質（不需要 edge 就能改善）
# ----------------------------------------------------------------------

def gate4_execution(sim, trades: pd.DataFrame, df: pd.DataFrame) -> list[str]:
    _hr("Gate 4　執行品質（不需要 edge 就能改善的漏水）")
    issues: list[str] = []

    # --- 4a 進場當天就停損 ---------------------------------------------
    # 先前的實測 bug：停損價用訊號價而非成交價算，部位在進場當天就被掃出場。
    if not trades.empty:
        same_day = _same_day_stops(trades)
        if same_day:
            # 當天進當天停損不一定是 bug。設定的停損是 -stop_loss_pct，
            # 標的當天就跌破那條線，出場就是正確行為。
            # 真正的 bug 長相不同：虧損幅度離設定值很遠（例如 -0.5% 或
            # 甚至還在賺的時候就被掃出場），那才代表停損價算錯了基準。
            pct = float(sim.cfg.get("stop_loss_pct", 0.07)) * 100
            losses = [abs(float(t.get("報酬率(%)", 0) or 0)) for t in same_day]
            suspicious = [t for t, L in zip(same_day, losses) if L < pct * 0.7]
            print(f"  · 進場當天就停損：{len(same_day)} 筆"
                  f"（停損設定 -{pct:.0f}%）")
            for t in same_day[:5]:
                r = t.get("報酬率(%)", float("nan"))
                tag = "  ← 可疑" if abs(float(r or 0)) < pct * 0.7 else ""
                print(f"      {t['策略']:18s} {t['symbol']:10s} {t['date']}  "
                      f"{r:+.2f}%{tag}")
            if suspicious:
                issues.append(
                    f"{len(suspicious)} 筆停損的虧損幅度遠小於設定的 -{pct:.0f}%")
                print("      ✗ 有幾筆的虧損幅度離設定值太遠——"
                      "停損價可能又是用訊號價而非成交價算的")
            else:
                print(f"      → 虧損幅度都接近或超過設定的 -{pct:.0f}%，"
                      "屬於標的當天急跌，不是停損價算錯")
        else:
            print("  ✓ 沒有進場當天就停損的部位")

    # --- 4b 持股檔數是否超過 top_n --------------------------------------
    top_n = sim.cfg.get("top_n")
    over = []
    for name, p in sim.portfolios.items():
        mx = max((e.get("n_positions", 0) for e in p.equity_curve), default=0)
        if top_n and mx > top_n:
            over.append((name, mx))
    if over:
        issues.append(f"{len(over)} 個策略持股檔數超過 top_n={top_n}")
        print(f"  ✗ 持股檔數超過 top_n={top_n}：")
        for name, mx in over:
            print(f"      {name:18s} 最多 {mx} 檔")
        print("      → 賣出訊號可能沒被執行（先前出現過買 7 賣 0 的 bug）")
    elif top_n:
        print(f"  ✓ 持股檔數沒有超過 top_n={top_n}")

    # --- 4c 賣出原因分布 -------------------------------------------------
    if not trades.empty and "reason" in trades.columns:
        sells = trades[trades["side"] == "賣出"]
        if not sells.empty:
            print("\n  出場原因分布")
            # 停損訊息帶著當天的價格，逐字分組會把同一種原因拆成很多列。
            # 截到第一個全形括號之前，才看得出真正的分布。
            sells = sells.assign(
                _reason=sells["reason"].astype(str).str.split("（").str[0].str.strip())
            for reason, g in sorted(sells.groupby("_reason"),
                                    key=lambda kv: -len(kv[1])):
                r = pd.to_numeric(g.get("報酬率(%)"), errors="coerce").dropna()
                avg = f"{r.mean():+.2f}%" if len(r) else "—"
                print(f"      {reason:28s} {len(g):>4d} 筆   平均 {avg}")
            stops = sells[sells["_reason"].str.contains("停損")]
            if not stops.empty:
                sr = pd.to_numeric(stops.get("報酬率(%)"), errors="coerce").dropna()
                want = -float(sim.cfg.get("stop_loss_pct", 0.07)) * 100
                if len(sr) >= 3 and sr.mean() < want - 1.0:
                    issues.append(
                        f"停損實際出場 {sr.mean():.2f}%，比設定的 {want:.0f}% 差"
                        f"{abs(sr.mean() - want):.1f} 個百分點（跳空穿價）")
                    print(f"\n      ✗ 停損設定 {want:.0f}%，實際平均出場 {sr.mean():.2f}%")
                    print(f"        差 {abs(sr.mean() - want):.1f} 個百分點——"
                          "停損是用收盤價檢查的，跳空的部分停不住。")
                    print("        這不是設定錯，是日 K 停損的固有限制："
                          "價格跳過停損線時，你只能在更低的地方出場。")
            stop_share = sells["_reason"].str.contains("停損").mean()
            if stop_share > 0.5:
                issues.append(f"停損出場佔 {stop_share * 100:.0f}%，停損可能設太緊")
                print(f"\n      ✗ 停損出場佔 {stop_share * 100:.0f}%——"
                      "超過一半的部位是被掃出場的")
                print("        這通常不是判斷錯，是停損倍數對標的波動度太緊")

    # --- 4d 紀錄檔資料品質 -----------------------------------------------
    print("\n  紀錄檔資料品質")
    if df is None or df.empty:
        print("      （沒有紀錄檔，跳過）")
        return issues

    n = len(df)
    for col, label in (("資料是否停滯", "資料停滯"),
                       ("是否較同批次落後", "較同批次落後")):
        if col in df.columns:
            bad = df[col].astype(str).str.lower().isin(("true", "是", "1")).sum()
            if bad:
                issues.append(f"{label} {bad}/{n} 筆")
                print(f"      ✗ {label}：{bad}/{n} 筆（{bad / n * 100:.0f}%）")
            else:
                print(f"      ✓ {label}：0 筆")

    if "跳過原因" in df.columns:
        skipped = df["跳過原因"].notna() & (df["跳過原因"].astype(str).str.strip() != "")
        cnt = int(skipped.sum())
        if cnt:
            issues.append(f"被跳過 {cnt}/{n} 筆")
            print(f"      ✗ 被跳過：{cnt}/{n} 筆（{cnt / n * 100:.0f}%）")
            for reason, c in df.loc[skipped, "跳過原因"].value_counts().head(3).items():
                print(f"          {str(reason)[:50]}  {c} 筆")
        else:
            print(f"      ✓ 被跳過：0 筆")

    key = [c for c in ("執行時間", "股票代碼") if c in df.columns]
    if len(key) == 2:
        dup = int(df.duplicated(subset=key).sum())
        if dup:
            issues.append(f"紀錄檔有 {dup} 筆重複列")
            print(f"      ✗ 重複列：{dup} 筆 → 跑 python stock.py repair")
        else:
            print("      ✓ 重複列：0 筆")

    # --- 4e 訊號價 vs 實際成交價的跳空滑價 --------------------------------
    slip = _gap_slippage(trades, df)
    if slip is not None and len(slip) >= 5:
        m, lo, hi, cnt = mean_ci(slip)
        sd = float(pd.Series(slip).std(ddof=1)) if cnt > 1 else 0.0
        print(f"\n  隔日開盤跳空滑價（訊號價 → 實際成交價）")
        print(f"      平均 {m:+.2f}%   95% 區間 [{lo:+.2f}%, {hi:+.2f}%]   n={cnt}")
        print(f"      最差 {min(slip):+.2f}%   最好 {max(slip):+.2f}%")
        # 隔夜跳空的標準差正常在 2% 上下。大幅超過代表配對到的不是同一次
        # 執行——最常見的原因是把新的 state.json 配上舊的紀錄檔。
        # 這種錯不會噴例外，只會讓上面的數字看起來有模有樣卻毫無意義。
        if sd > 5.0:
            issues.append(f"滑價離散度異常（標準差 {sd:.1f}%），紀錄檔與模擬可能不同步")
            print(f"      ✗ 標準差 {sd:.1f}%，遠高於隔夜跳空的正常範圍（約 2%）")
            print("        很可能是紀錄檔與 state.json 來自不同時期的執行。")
            print("        請確認兩個檔案是同一輪模擬產生的，否則這段數字不能採信。")
        elif m > 0.2:
            issues.append(f"平均買進滑價 {m:+.2f}%，吃掉約 {m / 0.471 * 100:.0f}% 的成本預算")
            print(f"      ✗ 平均買高了 {m:.2f}%——相當於額外付出"
                  f" {m / 0.471 * 100:.0f}% 的來回成本")
            print("        訊號在收盤後產生、隔天開盤才成交，跳空的部分是硬成本。")

    return issues


def _same_day_stops(trades: pd.DataFrame) -> list[dict]:
    """用 FIFO 配對買賣，找出當天進當天出的停損。"""
    out = []
    for strat, g in trades.groupby("策略"):
        pend: dict[str, deque] = defaultdict(deque)
        for _, t in g.sort_values("date").iterrows():
            sym = t["symbol"]
            if t["side"] == "買進":
                pend[sym].append(t["date"])
            elif t["side"] == "賣出" and pend[sym]:
                entry = pend[sym].popleft()
                if entry == t["date"] and "停損" in str(t.get("reason", "")):
                    out.append({"策略": strat, **t.to_dict()})
    return out


def _gap_slippage(trades: pd.DataFrame, df: pd.DataFrame):
    """
    比較「訊號當下的價格」與「隔天開盤實際成交價」。

    訊號在 D 日收盤後產生、D+1 開盤成交，中間的跳空是實打實的成本，
    不是模擬的誤差。量出來才知道它吃掉多少利潤。

    配對方式很重要：必須用 (股票代碼, 預測目標日) 對上 (symbol, 成交日)。
    早期版本拿整段期間的價格中位數當基準，那等於在量「這檔股票這兩週
    漲跌多少」，不是量滑價——算出來的數字看起來有模有樣，其實毫無意義。
    """
    if trades is None or trades.empty or df is None or df.empty:
        return None
    price_col = next((c for c in ("目前股價", "執行當下價格") if c in df.columns), None)
    if price_col is None or "股票代碼" not in df.columns:
        return None
    target_col = next((c for c in ("預測目標日(隔日估計)", "預測目標日")
                       if c in df.columns), None)
    if target_col is None:
        return None

    def _norm_sym(x):
        return str(x).strip().upper().replace(".TW", "").replace(".TWO", "")

    def _norm_date(x):
        if isinstance(x, (dt.datetime, pd.Timestamp)):
            return x.date().isoformat()
        if isinstance(x, dt.date):
            return x.isoformat()
        txt = str(x).strip()
        return txt[:10] if len(txt) >= 10 else txt

    ref: dict[tuple[str, str], float] = {}
    for _, r in df.iterrows():
        px = pd.to_numeric(r.get(price_col), errors="coerce")
        if pd.isna(px) or px <= 0:
            continue
        ref[(_norm_sym(r["股票代碼"]), _norm_date(r[target_col]))] = float(px)
    if not ref:
        return None

    slips = []
    buys = trades[trades["side"] == "買進"]
    for _, t in buys.iterrows():
        base = ref.get((_norm_sym(t["symbol"]), _norm_date(t["date"])))
        fill = pd.to_numeric(t.get("price"), errors="coerce")
        if base and not pd.isna(fill) and fill > 0:
            slips.append((float(fill) / base - 1) * 100)
    return slips or None


# ----------------------------------------------------------------------
# 總結
# ----------------------------------------------------------------------

def final_verdict(g1, g2, g3, issues: list[str] | None):
    _hr("結論")
    print(f"  Gate 1  模型有沒有 edge        {verdict(g1)}")
    print(f"  Gate 2  有沒有贏過不做         {verdict(g2)}")
    print(f"  Gate 3  成本是否覆蓋得住       {verdict(g3)}")
    if issues is None:
        g4_text = "未執行（缺少模擬狀態或紀錄檔）"
    elif issues:
        g4_text = f"發現 {len(issues)} 項問題"
    else:
        g4_text = "已檢查，無明顯問題"
    print(f"  Gate 4  執行品質               {g4_text}")

    print("\n  建議")
    print("  " + "-" * 66)
    if g1 is False:
        print("  ▸ 不要調模型參數、不要加特徵。兩週的資料調參數是對雜訊過擬合，")
        print("    會讓回測變漂亮、實盤變差。")
        print("  ▸ 改做不需要 edge 也有效的三件事：")
        print("      1. 降低周轉（提高 max_holding_days、拉高進場門檻）——直接減成本")
        print("      2. 部位大小依證據強度調整（Wilson 下界低的少押或不押）")
        print("      3. 事件日不開新倉（python stock.py events --stats 有樣本後再啟用）")
    elif g1 is None:
        print("  ▸ 紀錄檔缺少信賴下限欄位，先確認紀錄檔版本與完整性。")
    else:
        print("  ▸ 模型有 edge。接下來看 Gate 2/3 決定是執行面還是成本面的問題。")

    if issues:
        print("\n  ▸ Gate 4 的問題優先修——那是漏水，不需要 edge 就能補：")
        for i in issues:
            print(f"      · {i}")

    if g1 is not True and g2 is not True:
        print("\n  ▸ 關於真錢：目前沒有證據支持這套系統有獲利能力。")
        print("    誠實的做法是繼續模擬累積樣本，而不是先下小錢試試看——")
        print("    小錢一樣會虧，而且虧的速度不足以讓你及時發現問題。")
    print()


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

LOG_EXCLUDE = ("備份", "old file", "fallback", "已修復", "修復前", "加建議前")


def find_log(explicit=None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    cands = [p for p in ROOT.glob("stock_analysis_log*.xlsx")
             if not any(k in p.name for k in LOG_EXCLUDE)]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def find_state(explicit=None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    for c in (ROOT / "tools" / "paper_trading_state.json",
              ROOT / "paper_trading_state.json"):
        if c.exists():
            return c
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="獲利能力評估：四道關卡")
    ap.add_argument("--log", help="分析紀錄檔（預設自動尋找）")
    ap.add_argument("--state", help="模擬狀態檔（預設自動尋找）")
    args = ap.parse_args(argv)

    _hr("獲利能力評估")
    print(f"  執行時間 {dt.datetime.now():%Y-%m-%d %H:%M}")
    wilson, wsrc = get_wilson()
    print(f"  Wilson 下界公式來源：{wsrc}")
    print("\n  判斷標準在看到資料之前就寫死了：")
    print(f"    Gate 1  Wilson 下界中位數 > {GATE1_MIN_LOWER_BOUND:.0f}%")
    print("    Gate 2  同時贏過 cash 與 active_equal")
    print(f"    Gate 3  每筆平均毛利 > 來回成本 × {GATE3_COST_SAFETY}")
    print(f"    樣本門檻  少於 {MIN_TRADES_FOR_CONCLUSION} 筆一律回報「無法判斷」")

    log_path = find_log(args.log)
    state_path = find_state(args.state)
    print(f"\n  紀錄檔：{log_path.name if log_path else '找不到'}")
    print(f"  狀態檔：{state_path if state_path else '找不到'}")

    df = pd.DataFrame()
    if log_path:
        try:
            df = read_log(log_path)
            print(f"          {len(df)} 列 / {len(df.columns)} 欄")
        except Exception as e:                                     # noqa: BLE001
            print(f"  ✗ 讀取紀錄檔失敗：{type(e).__name__}: {e}")

    g1 = gate1_edge(df) if not df.empty else None
    if df.empty:
        _hr("Gate 1　模型有沒有 edge")
        print("  （沒有紀錄檔，跳過）")

    g2 = g3 = None
    # None = 這一關根本沒跑；[] = 跑了而且沒發現問題。
    # 兩者絕不能混為一談——把「沒檢查」報成「沒問題」是最糟的一種錯誤訊息。
    issues: list[str] | None = None
    if state_path:
        sys.path.insert(0, str(state_path.parent))
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            pt_path = find_file("paper_trading.py")
            if pt_path is None:
                raise FileNotFoundError(
                    "找不到 paper_trading.py（已在主資料夾與 tools\\ 都找過）")
            pt = _load_module(pt_path, "paper_trading")
            sim = pt.Simulator.load(state_path)
        except Exception as e:                                     # noqa: BLE001
            print(f"\n  ✗ 載入模擬狀態失敗：{type(e).__name__}: {e}")
            print("     Gate 2 / 3 無法執行。Gate 4 改用紀錄檔能檢查的部分。")
            sim = None
        if sim is not None:
            trades = sim.all_trades()
            g2 = gate2_benchmark(sim)
            g3 = gate3_cost(sim, trades)
            issues = gate4_execution(sim, trades, df)
        elif not df.empty:
            issues = gate4_execution_logonly(df)
    else:
        _hr("Gate 2 / 3")
        print("  （找不到 paper_trading_state.json，跳過）")
        print("  建立模擬：python stock.py sim-init")
        if not df.empty:
            issues = gate4_execution_logonly(df)

    final_verdict(g1, g2, g3, issues)
    return 0


def gate4_execution_logonly(df: pd.DataFrame) -> list[str]:
    """沒有模擬狀態時，只做紀錄檔那半邊的品質檢查。"""
    class _Empty:
        cfg = {}
        portfolios = {}
    return gate4_execution(_Empty(), pd.DataFrame(), df)


if __name__ == "__main__":
    sys.exit(main())
