# -*- coding: utf-8 -*-
"""
紙上交易模擬器離線測試
======================
不連網路，用合成價格 + 你真實的 stock_analysis_log 跑完整週流程。

執行：python tests/test_paper_trading.py
"""

import datetime as dt
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SIM_PATH = ROOT / "tools" / "paper_trading.py"
SAMPLE_LOG = ROOT / "samples" / "stock_analysis_log_20260828.xlsx"

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def load_sim():
    spec = importlib.util.spec_from_file_location("paper_trading", SIM_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_prices(symbols, dates, seed=11, drift=0.0):
    """合成每日開/收盤價。"""
    rng = np.random.default_rng(seed)
    rows = []
    base = {s: float(rng.uniform(20, 600)) for s in symbols}
    for d in dates:
        for s in symbols:
            o = base[s] * (1 + rng.normal(drift, 0.012))
            c = o * (1 + rng.normal(drift, 0.015))
            rows.append({"date": d.isoformat(), "symbol": s, "open": o, "close": c})
            base[s] = c
    return pd.DataFrame(rows)


def main():
    print("=" * 62)
    print("  紙上交易模擬器 離線測試")
    print("=" * 62)
    m = load_sim()

    # ---------------------------------------------------------
    print("\n[1] 成本模型")
    check("小額買進吃到最低手續費 20 元", abs(m.buy_cost(1000) - 20.0) < 1e-9, m.buy_cost(1000))
    check("大額買進 = 金額×0.1425%×6折",
          abs(m.buy_cost(1_000_000) - 1_000_000 * 0.001425 * 0.6) < 1e-6)
    # 賣出 100 萬：手續費 855 + 證交稅 3000
    check("賣出含證交稅 0.3%", abs(m.sell_cost(1_000_000) - (855 + 3000)) < 1e-6,
          m.sell_cost(1_000_000))
    check("來回成本 ≈ 0.471%", abs(m.round_trip_cost_pct() - 0.471) < 1e-9)

    # ---------------------------------------------------------
    print("\n[2] 投資組合基本操作")
    p = m.Portfolio("t", 1_000_000)
    ok = p.execute_buy("2330.TW", 100, 2400.0, dt.date(2026, 8, 31), "測試")
    check("買進 100 股 @2400 成功", ok)
    check("現金正確扣除（含手續費）",
          abs(p.cash - (1_000_000 - 240_000 - m.buy_cost(240_000))) < 1e-6, p.cash)
    check("部位建立正確", p.positions["2330.TW"]["shares"] == 100)
    ok = p.execute_buy("2330.TW", 100, 999_999.0, dt.date(2026, 8, 31), "買不起")
    check("資金不足時買進失敗", not ok)
    p.execute_sell("2330.TW", 2500.0, dt.date(2026, 9, 1), "測試賣出")
    check("賣出後部位清空", "2330.TW" not in p.positions)
    t = p.trades[-1]
    check("已實現損益已扣賣出成本",
          abs(t["已實現損益"] - ((2500 - 2400) * 100 - m.sell_cost(250_000))) < 1e-6,
          t["已實現損益"])
    check("報酬率記錄正確", abs(t["報酬率(%)"] - (2500 / 2400 - 1) * 100) < 1e-9)

    # ---------------------------------------------------------
    print("\n[3] 選股規則（用你真實的 log）")
    check("樣本 log 存在", SAMPLE_LOG.exists())
    sig = m._clean_signals(SAMPLE_LOG, dt.date(2026, 8, 28))
    check("能取出 2026-08-28 的訊號", len(sig) > 0, f"n={len(sig)}")
    check("同一檔只留一筆（已去重）", sig["股票代碼"].is_unique)

    sd = m.pick_targets("strategy_decision", sig, 5)
    check(f"strategy_decision 選出 {len(sd)} 檔（現行規則幾乎不出手）", isinstance(sd, list))
    sc = m.pick_targets("score_topn", sig, 5)
    check("score_topn 有選出標的", len(sc) > 0, f"{sc}")
    check("score_topn 按綜合分數排序（首檔分數最高）",
          len(sc) > 0 and sc[0][0] in sig.sort_values("綜合分數", ascending=False)
          ["股票代碼"].head(3).tolist(), f"{sc[:2]}")
    pr = m.pick_targets("prob_topn", sig, 5)
    check("prob_topn 有選出標的", len(pr) > 0)
    check("cash 策略永遠不選股", m.pick_targets("cash", sig, 5) == [])

    # ---------------------------------------------------------
    print("\n[4] 端到端：跑完下週 5 個交易日")
    week = [dt.date(2026, 8, 31), dt.date(2026, 9, 1), dt.date(2026, 9, 2),
            dt.date(2026, 9, 3), dt.date(2026, 9, 4)]
    symbols = sorted(sig["股票代碼"].unique().tolist())
    prices = make_prices(symbols, [dt.date(2026, 8, 28)] + week, drift=0.001)

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "s.json"
        pf = Path(td) / "px.csv"
        prices.to_csv(pf, index=False)

        rc = m.main(["init", "--state", str(state), "--capital", "1000000"])
        check("init 成功", rc == 0 and state.exists())

        # 8/28（上週五）收盤後：產生下週一的單
        rc = m.main(["step", "--state", str(state), "--date", "2026-08-28",
                     "--log", str(SAMPLE_LOG), "--offline-prices", str(pf)])
        check("8/28 產生下週一委託單", rc == 0)
        st = json.loads(state.read_text(encoding="utf-8"))
        check("score_topn 已有待成交單",
              len(st["portfolios"]["score_topn"]["pending"]) > 0,
              str(st["portfolios"]["score_topn"]["pending"]))
        check("cash 策略沒有任何待成交單",
              len(st["portfolios"]["cash"]["pending"]) == 0)

        # 下週一~週四
        for d in week[:-1]:
            rc = m.main(["step", "--state", str(state), "--date", d.isoformat(),
                         "--log", str(SAMPLE_LOG), "--offline-prices", str(pf)])
            check(f"{d} 執行成功", rc == 0)

        # 週五結算
        rc = m.main(["settle", "--state", str(state), "--date", "2026-09-04",
                     "--log", str(SAMPLE_LOG), "--offline-prices", str(pf)])
        check("9/4 結算成功", rc == 0)

        sim = m.Simulator.load(state)
        check("已標記為結算完成", sim.state["settled"] is True)
        check("處理了 6 個日期（8/28 + 下週 5 天）",
              len(sim.state["processed_dates"]) == 6,
              str(sim.state["processed_dates"]))

        for name, p in sim.portfolios.items():
            check(f"{name}: 結算後無剩餘部位", len(p.positions) == 0,
                  str(p.positions))

        trades = sim.all_trades()
        if not trades.empty:
            same_day = trades.groupby(["策略", "symbol", "date"]).size()
            check("沒有「買進當天就被停損掃出」的異常（回歸測試）",
                  (same_day <= 1).all(),
                  str(same_day[same_day > 1].head()))

        s = sim.summary()
        check("報告含全部 6 個策略", len(s) == 6)
        cash_ret = float(s[s["策略"] == "cash"]["報酬率(%)"].iloc[0])
        check("cash 策略報酬率為 0（什麼都沒做）", abs(cash_ret) < 1e-9, cash_ret)
        check("有交易的策略累計成本 > 0",
              float(s[s["策略"] == "score_topn"]["累計交易成本"].iloc[0]) > 0)

        # 重複執行同一天不應重複計算
        before = sim.portfolios["score_topn"].cash
        m.main(["step", "--state", str(state), "--date", "2026-09-04",
                "--log", str(SAMPLE_LOG), "--offline-prices", str(pf)])
        sim2 = m.Simulator.load(state)
        check("重複執行同一天不會重複計算",
              abs(sim2.portfolios["score_topn"].cash - before) < 1e-9)

        out = Path(td) / "rep.xlsx"
        m.main(["report", "--state", str(state), "-o", str(out)])
        check("報告 Excel 產生成功", out.exists())
        sheets = pd.ExcelFile(out).sheet_names
        check("報告含策略績效/交易明細/每日權益",
              all(x in sheets for x in ["策略績效", "每日權益", "模擬參數"]), str(sheets))

    # ---------------------------------------------------------
    print("\n[5] 停損機制")
    sim = m.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)

    check("停損價由成交價往下算（百分比）",
          abs(sim._stop_price_from_fill(100.0, None) - 93.0) < 1e-9,
          sim._stop_price_from_fill(100.0, None))
    check("ATR 停損較近時採用 ATR（100 - 2×2 = 96 > 93）",
          abs(sim._stop_price_from_fill(100.0, 2.0) - 96.0) < 1e-9,
          sim._stop_price_from_fill(100.0, 2.0))
    check("ATR 停損較遠時採用百分比（100 - 2×10 = 80 < 93）",
          abs(sim._stop_price_from_fill(100.0, 10.0) - 93.0) < 1e-9,
          sim._stop_price_from_fill(100.0, 10.0))
    check("停損價必定低於成交價（回歸測試：跳空造成的荒謬停損價）",
          all(sim._stop_price_from_fill(px, atr) < px
              for px in (10.0, 100.0, 2400.0) for atr in (None, 0.5, 5.0, 999.0)))

    p = sim.portfolios["score_topn"]
    p.execute_buy("TEST.TW", 1000, 100.0, dt.date(2026, 8, 31), "測試",
                  stop_price=sim._stop_price_from_fill(100.0, None))
    stopped = sim._check_stops(p, {"TEST.TW": {"close": 92.0}},
                               dt.date(2026, 9, 1), 0.6)
    check("跌破 -7% 觸發停損", stopped == 1 and "TEST.TW" not in p.positions)

    p2 = sim.portfolios["prob_topn"]
    p2.execute_buy("TEST2.TW", 1000, 100.0, dt.date(2026, 8, 31), "測試",
                   stop_price=sim._stop_price_from_fill(100.0, None))
    stopped = sim._check_stops(p2, {"TEST2.TW": {"close": 96.0}},
                               dt.date(2026, 9, 1), 0.6)
    check("跌 -4% 不觸發停損", stopped == 0 and "TEST2.TW" in p2.positions)

    # 舊狀態檔或異常 stop_price（高於均價）必須被保底規則接管
    p3 = sim.portfolios["ev_decision"]
    p3.execute_buy("TEST3.TW", 1000, 100.0, dt.date(2026, 8, 31), "測試",
                   stop_price=2300.0)   # 荒謬值：高於成交價
    stopped = sim._check_stops(p3, {"TEST3.TW": {"close": 99.0}},
                               dt.date(2026, 9, 1), 0.6)
    check("停損價高於均價時改用保底規則，不會誤觸發",
          stopped == 0 and "TEST3.TW" in p3.positions)

    print("\n" + "=" * 62)
    print(f"  通過 {_passed} 項 / 失敗 {_failed} 項")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
