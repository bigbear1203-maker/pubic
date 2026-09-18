# -*- coding: utf-8 -*-
"""
變體策略離線測試
================
三個變體各自只改 score_topn 的「一個」變數。這份測試的核心就是驗證
那句話成立——如果變體同時改了兩件事，之後看到績效差異也歸因不了。

    score_lowturn  只改出場規則（周轉）
    score_limit    只改成交條件（進場價）
    etf_hold       買進持有大盤（對照基準）

執行：python tests/test_variants.py
"""

import datetime as dt
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd


def _find_root() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent, Path.cwd()):
        if (cand / "stock.py").exists():
            return cand
    return here.parent


ROOT = _find_root()
_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def load_pt():
    for cand in (ROOT / "tools" / "paper_trading.py", ROOT / "paper_trading.py"):
        if cand.exists():
            spec = importlib.util.spec_from_file_location("paper_trading", cand)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise FileNotFoundError("找不到 paper_trading.py")


def make_signals(scores, price=100.0, atr=2.5):
    """綜合分數由高到低的訊號表。"""
    return pd.DataFrame([
        {"股票代碼": f"{2300 + i}.TW", "綜合分數": sc,
         "目前股價": price + i, "ATR": atr,
         "隔日_邏輯迴歸_上漲機率(%)": 50 + sc}
        for i, sc in enumerate(scores)
    ])


# ----------------------------------------------------------------------
def test_registry(m):
    print("\n[1] 註冊")
    for name in ("score_lowturn", "score_limit", "etf_hold"):
        check(f"{name} 在 STRATEGIES 裡", name in m.STRATEGIES)
        check(f"{name} 有說明文字", name in m.STRATEGY_DESC)
    check("score_lowturn / score_limit 屬於排名型",
          "score_lowturn" in m.RANKING_STRATEGIES
          and "score_limit" in m.RANKING_STRATEGIES)
    check("etf_hold 屬於買進持有型", "etf_hold" in m.BUY_HOLD_STRATEGIES)
    check("etf_hold 不屬於排名型或訊號型",
          "etf_hold" not in m.RANKING_STRATEGIES
          and "etf_hold" not in m.SIGNAL_STRATEGIES)
    check("原本 6 個策略都還在",
          all(n in m.STRATEGIES for n in
              ("strategy_decision", "ev_decision", "score_topn",
               "prob_topn", "active_equal", "cash")))


def test_same_selection(m):
    """三個 score 變體的選股必須完全一樣，差別只能在別的地方。"""
    print("\n[2] 選股邏輯：三個變體必須挑出完全相同的股票")
    sig = make_signals([8, 6, 5, 3, 2, 1, -1, -3])
    base = m.pick_targets("score_topn", sig, 5)
    for variant in ("score_lowturn", "score_limit"):
        got = m.pick_targets(variant, sig, 5)
        check(f"{variant} 與 score_topn 選股相同",
              [x[0] for x in got] == [x[0] for x in base],
              f"{[x[0] for x in got]} vs {[x[0] for x in base]}")
    check("選出 5 檔", len(base) == 5, str(len(base)))
    check("只選綜合分數 > 0 的", all("-" not in r for _, r in base))


def test_etf_targets(m):
    print("\n[3] etf_hold 不依賴訊號")
    check("有訊號時選 ETF",
          m.pick_targets("etf_hold", make_signals([5, 3]), 5)
          == [(m.ETF_SYMBOL, f"買進並持有 {m.ETF_SYMBOL}（大盤對照組）")])
    empty = m.pick_targets("etf_hold", pd.DataFrame(), 5)
    check("訊號表是空的仍然選 ETF（分析程式沒跑也要照買）",
          len(empty) == 1 and empty[0][0] == m.ETF_SYMBOL, str(empty))
    check("cash 在訊號空時仍回空", m.pick_targets("cash", pd.DataFrame(), 5) == [])
    check("score_topn 在訊號空時回空",
          m.pick_targets("score_topn", pd.DataFrame(), 5) == [])


def test_lowturn_exits(m):
    print("\n[4] score_lowturn：只改出場規則")
    # 需要至少 top_n×LOWTURN_EXIT_RANK_MULT 檔「綜合分數 > 0」，
    # 否則寬名次帶會被可選檔數而不是被規則限制住。
    sig = make_signals([14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, -1, -2])
    syms = sig["股票代碼"].tolist()
    top5 = m.pick_targets("score_topn", sig, 5)
    today = dt.date(2026, 9, 18)
    # 持有第 6、7 名（跌出前 5，但還在前 10）
    positions = {syms[5]: {"shares": 100, "avg_cost": 100.0,
                           "entry_date": "2026-09-17", "stop_price": 93.0},
                 syms[6]: {"shares": 100, "avg_cost": 100.0,
                           "entry_date": "2026-09-17", "stop_price": 93.0}}

    base_exits = m.pick_exits("score_topn", sig, positions, top5, 10, today)
    check("score_topn：跌出前 5 名就換掉（2 檔都賣）",
          len(base_exits) == 2, str(base_exits))

    keep = m.keep_universe("score_lowturn", sig, 5)
    check(f"低周轉的保留名單是前 {5 * m.LOWTURN_EXIT_RANK_MULT} 名",
          len(keep) == 5 * m.LOWTURN_EXIT_RANK_MULT, str(len(keep)))
    check("保留名單是前 5 名的超集（不會把持股中的前段班賣掉）",
          {x[0] for x in top5}.issubset(keep))
    check("其他排名型策略的保留名單是空的（沿用原本的前 N 名規則）",
          m.keep_universe("score_topn", sig, 5) == set()
          and m.keep_universe("prob_topn", sig, 5) == set())
    low_exits = m.pick_exits("score_lowturn", sig, positions, top5, 10, today,
                             keep=keep)
    check("score_lowturn：還在前 10 名就不換（0 檔賣出）",
          len(low_exits) == 0, str(low_exits))

    # 真的跌出寬名次帶就該賣
    far = {syms[11]: {"shares": 100, "avg_cost": 100.0,
                     "entry_date": "2026-09-17", "stop_price": 93.0}}
    check("跌出前 10 名仍然會換掉",
          len(m.pick_exits("score_lowturn", sig, far, top5, 10, today,
                           keep=keep)) == 1)

    check(f"最長持有天數加倍（10 → {10 * m.LOWTURN_HOLD_MULT}）",
          m.holding_days_for("score_lowturn", 10) == 10 * m.LOWTURN_HOLD_MULT)
    check("其他策略的持有天數不變",
          m.holding_days_for("score_topn", 10) == 10
          and m.holding_days_for("prob_topn", 10) == 10)

    # 時間停損仍然有效
    old_pos = {syms[0]: {"shares": 100, "avg_cost": 100.0,
                         "entry_date": "2026-08-01", "stop_price": 93.0}}
    ex = m.pick_exits("score_lowturn", sig, old_pos, top5, 10, today, keep=keep)
    check("低周轉仍受時間停損保護（不會永久卡住）",
          len(ex) == 1 and "時間停損" in list(ex.values())[0], str(ex))


def test_buyhold_never_exits(m):
    print("\n[5] etf_hold：不換股、不時間停損、不停損")
    positions = {m.ETF_SYMBOL: {"shares": 100, "avg_cost": 100.0,
                                "entry_date": "2026-01-01", "stop_price": 93.0}}
    ex = m.pick_exits("etf_hold", make_signals([5, 3]), positions, [], 10,
                      dt.date(2026, 9, 18))
    check("持有超過 260 天也不出場（買進持有就是不動）", ex == {}, str(ex))

    sim = m.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    p = sim.portfolios["etf_hold"]
    p.positions = dict(positions)
    # 收盤價遠低於停損價
    n = sim._check_stops(p, {m.ETF_SYMBOL: {"close": 50.0}},
                         dt.date(2026, 9, 18), 0.6)
    check("腰斬也不停損", n == 0 and m.ETF_SYMBOL in p.positions, str(n))

    p2 = sim.portfolios["score_topn"]
    p2.positions = dict(positions)
    n2 = sim._check_stops(p2, {m.ETF_SYMBOL: {"close": 50.0}},
                          dt.date(2026, 9, 18), 0.6)
    check("同樣情況下 score_topn 會停損（確認上面不是因為檢查失效）", n2 == 1)


def test_limit_orders(m):
    print("\n[6] score_limit：只改成交條件")
    sim = m.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    sig = make_signals([9, 8, 7], price=100.0)
    p = sim.portfolios["score_limit"]
    orders = sim._build_orders("score_limit", p, sig, {}, dt.date(2026, 9, 18))
    buys = [o for o in orders if o["side"] == "buy"]
    check("限價變體的買單帶 limit", all("limit" in o for o in buys), str(buys[:1]))
    check("limit = 訊號日股價",
          abs(buys[0]["limit"] - float(sig.iloc[0]["目前股價"])) < 1e-9,
          f"{buys[0]['limit']} vs {sig.iloc[0]['目前股價']}")

    p3 = sim.portfolios["score_topn"]
    base = sim._build_orders("score_topn", p3, sig, {}, dt.date(2026, 9, 18))
    check("score_topn 的買單沒有 limit",
          all("limit" not in o for o in base if o["side"] == "buy"))
    check("兩者買的是同一批股票",
          sorted(o["symbol"] for o in buys)
          == sorted(o["symbol"] for o in base if o["side"] == "buy"))


def test_limit_fill_behaviour(m):
    print("\n[7] 端到端：開高不追，開低才買")
    syms = ["2300.TW", "2301.TW", "2302.TW"]
    d0, d1 = dt.date(2026, 9, 17), dt.date(2026, 9, 18)
    cols = ["股票代碼", "綜合分數", "目前股價", "ATR", "執行時間",
            "股價日期(資料基準日)", "預測目標日(隔日估計)"]
    rows = []
    for i, s in enumerate(syms):
        rows.append({"股票代碼": s, "綜合分數": 9 - i, "目前股價": 100.0,
                     "ATR": 2.5,
                     "執行時間": dt.datetime.combine(d0, dt.time(15, 10)),
                     "股價日期(資料基準日)": d0.isoformat(),
                     "預測目標日(隔日估計)": d1.isoformat()})
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        log = td / "stock_analysis_log_v3.7.xlsx"
        pd.DataFrame(rows)[cols].to_excel(log, index=False, sheet_name="分析紀錄")
        # 2300 開高 3%（不該成交）、2301 開平、2302 開低 2%
        px = []
        for d in (d0, d1):
            for i, s in enumerate(syms):
                o = 100.0 if d == d0 else (103.0, 100.0, 98.0)[i]
                px.append({"date": d.isoformat(), "symbol": s,
                           "open": o, "close": 100.0})
        pf = td / "px.csv"
        pd.DataFrame(px).to_csv(pf, index=False)
        state = td / "s.json"
        m.main(["init", "--state", str(state), "--capital", "1000000",
                "--top-n", "3"])
        m.main(["step", "--state", str(state), "--date", d0.isoformat(),
                "--log", str(log), "--offline-prices", str(pf)])
        m.main(["step", "--state", str(state), "--date", d1.isoformat(),
                "--log", str(log), "--offline-prices", str(pf)])
        sim = m.Simulator.load(state)

        lim = sim.portfolios["score_limit"]
        base = sim.portfolios["score_topn"]
        check("score_topn 三檔全買（不管開高開低）",
              len(base.positions) == 3, str(list(base.positions)))
        check("score_limit 沒買開高 3% 的 2300.TW",
              "2300.TW" not in lim.positions, str(list(lim.positions)))
        check("score_limit 買了開平的 2301.TW", "2301.TW" in lim.positions)
        check("score_limit 買了開低的 2302.TW", "2302.TW" in lim.positions)

        fills = {t["symbol"]: t["price"] for t in lim.trades if t["side"] == "買進"}
        check("成交價都不高於訊號價 100",
              all(v <= 100.0 + 1e-9 for v in fills.values()), str(fills))


def _write_history(path, rows):
    cols = ["股票代碼", "股價日期(資料基準日)", "目前股價", "綜合分數",
            "執行時間", "預測目標日(隔日估計)", "ATR",
            "外資買賣超佔當日成交量比重(%)"]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df[cols].to_excel(path, index=False, sheet_name="分析紀錄")


def test_trailing_returns(m):
    print("\n[9] 近 N 日報酬：只能用過去，不能碰未來")
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "stock_analysis_log_v3.7.xlsx"
        rows = []
        # A 從 100 一路跌到 90；B 從 100 一路漲到 110
        prices = {"A.TW": [100, 98, 96, 94, 92, 90],
                  "B.TW": [100, 102, 104, 106, 108, 110]}
        days = [dt.date(2026, 9, 7) + dt.timedelta(days=i) for i in range(6)]
        for sym, series in prices.items():
            for d, px in zip(days, series):
                rows.append({"股票代碼": sym,
                             "股價日期(資料基準日)": d.isoformat(),
                             "目前股價": px, "綜合分數": 3})
        _write_history(log, rows)

        tr = m.trailing_returns(log, days[-1], lookback=5)
        check("A 近 5 日 -10%", abs(tr["A.TW"] - (-10.0)) < 1e-9, str(tr))
        check("B 近 5 日 +10%", abs(tr["B.TW"] - 10.0) < 1e-9, str(tr))

        # 基準日往前挪，結果必須跟著變小——證明沒有偷看未來
        mid = m.trailing_returns(log, days[2], lookback=5)
        check("基準日 = 第 3 天時，A 只算得到 -4%（沒有偷看後面）",
              abs(mid["A.TW"] - (-4.0)) < 1e-9, str(mid))
        check("基準日 = 第 3 天時，B 只算得到 +4%",
              abs(mid["B.TW"] - 4.0) < 1e-9, str(mid))

        # 觀測點太少不給數字
        few = m.trailing_returns(log, days[1], lookback=5)
        check(f"只有 2 個觀測點時不回傳（門檻 {m.REVERSAL_MIN_OBS}）",
              few == {}, str(few))

        check("紀錄檔不存在時回傳空 dict 而不是炸掉",
              m.trailing_returns(Path(td) / "無.xlsx", days[-1]) == {})


def test_reversal_strategy(m):
    print("\n[10] reversal_topn：買跌最多的，與 score_topn 反向")
    sig = pd.DataFrame([
        {"股票代碼": "A.TW", "綜合分數": 1},
        {"股票代碼": "B.TW", "綜合分數": 9},
        {"股票代碼": "C.TW", "綜合分數": 5},
        {"股票代碼": "D.TW", "綜合分數": 7},
    ])
    trailing = {"A.TW": -8.0, "B.TW": +6.0, "C.TW": -3.0, "D.TW": +1.0}
    got = m.pick_targets("reversal_topn", sig, 2, trailing)
    check("選出跌最多的兩檔 A、C",
          [x[0] for x in got] == ["A.TW", "C.TW"], str(got))
    check("理由帶出報酬數字", "-8.0%" in got[0][1], str(got[0]))

    base = m.pick_targets("score_topn", sig, 2)
    check("score_topn 選的是分數最高的 B、D",
          [x[0] for x in base] == ["B.TW", "D.TW"], str(base))
    check("兩者選出的股票完全不重疊（確認方向真的相反）",
          not ({x[0] for x in got} & {x[0] for x in base}))

    check("沒有回看報酬時不選股（寧可不出手）",
          m.pick_targets("reversal_topn", sig, 2, {}) == [])
    check("回看報酬是 None 時也不選",
          m.pick_targets("reversal_topn", sig, 2, None) == [])
    partial = m.pick_targets("reversal_topn", sig, 3, {"C.TW": -3.0})
    check("只有部分標的算得出報酬時，只從那些裡面挑",
          [x[0] for x in partial] == ["C.TW"], str(partial))


def test_foreign_flow(m):
    print("\n[11] foreign_flow：跟隨外資買超")
    col = "外資買賣超佔當日成交量比重(%)"
    sig = pd.DataFrame([
        {"股票代碼": "A.TW", "綜合分數": 1, col: 2.5},
        {"股票代碼": "B.TW", "綜合分數": 9, col: -4.0},
        {"股票代碼": "C.TW", "綜合分數": 5, col: 8.1},
        {"股票代碼": "D.TW", "綜合分數": 7, col: None},
    ])
    got = m.pick_targets("foreign_flow", sig, 3)
    check("依外資買超佔量由高到低選 C、A",
          [x[0] for x in got] == ["C.TW", "A.TW"], str(got))
    check("外資賣超的 B 不選（只跟買超）",
          all(x[0] != "B.TW" for x in got))
    check("欄位是空值的 D 不選", all(x[0] != "D.TW" for x in got))
    check("缺整個欄位時回空，不炸掉",
          m.pick_targets("foreign_flow", sig.drop(columns=[col]), 3) == [])


def test_etf_end_to_end(m):
    print("\n[12] etf_hold 端到端：買一次，然後全程不動")
    syms = ["2300.TW", "2301.TW"]
    days = [dt.date(2026, 9, 14) + dt.timedelta(days=i) for i in range(5)]
    days = [d for d in days if d.weekday() < 5]
    cols = ["股票代碼", "綜合分數", "目前股價", "ATR", "執行時間",
            "股價日期(資料基準日)", "預測目標日(隔日估計)"]
    rows, px = [], []
    for i, d in enumerate(days):
        for j, sym in enumerate(syms):
            rows.append({"股票代碼": sym, "綜合分數": 5 - j, "目前股價": 100.0,
                         "ATR": 2.5,
                         "執行時間": dt.datetime.combine(d, dt.time(15, 10)),
                         "股價日期(資料基準日)": d.isoformat(),
                         "預測目標日(隔日估計)": d.isoformat()})
            px.append({"date": d.isoformat(), "symbol": sym,
                       "open": 100.0, "close": 100.0})
        # 0050 一路漲：最後權益要看得出來
        px.append({"date": d.isoformat(), "symbol": m.ETF_SYMBOL,
                   "open": 100.0 + i * 2, "close": 100.0 + i * 2})

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        log = td / "stock_analysis_log_v3.7.xlsx"
        pd.DataFrame(rows)[cols].to_excel(log, index=False, sheet_name="分析紀錄")
        pf = td / "px.csv"
        pd.DataFrame(px).to_csv(pf, index=False)
        state = td / "s.json"
        m.main(["init", "--state", str(state), "--capital", "1000000",
                "--top-n", "2"])
        for d in days:
            m.main(["step", "--state", str(state), "--date", d.isoformat(),
                    "--log", str(log), "--offline-prices", str(pf)])
        sim = m.Simulator.load(state)
        etf = sim.portfolios["etf_hold"]

        buys = [t for t in etf.trades if t["side"] == "買進"]
        sells = [t for t in etf.trades if t["side"] == "賣出"]
        check("etf_hold 有買進", len(buys) >= 1, str(etf.trades))
        check("買的是 0050", all(t["symbol"] == m.ETF_SYMBOL for t in buys))
        check("全程沒有賣出（買進持有）", len(sells) == 0, str(sells))
        check("只買一次，不重複加碼", len(buys) == 1, f"{len(buys)} 次")
        check("持股只有 0050 一檔", list(etf.positions) == [m.ETF_SYMBOL],
              str(list(etf.positions)))

        # 單一部位不該被 max_position_pct=25% 限制住
        cost = buys[0]["amount"]
        check("資金大部分投入（不受單檔 25% 上限限制）",
              cost > 1_000_000 * 0.9, f"投入 {cost:,.0f}")

        s = sim.summary()
        ret = float(s[s["策略"] == "etf_hold"]["報酬率(%)"].iloc[0])
        check("0050 上漲時 etf_hold 報酬為正", ret > 0, f"{ret:.2f}%")

        cash_ret = float(s[s["策略"] == "cash"]["報酬率(%)"].iloc[0])
        check("且贏過 cash（這正是它存在的意義）", ret > cash_ret,
              f"etf {ret:.2f}% vs cash {cash_ret:.2f}%")


def test_backward_compat(m):
    print("\n[13] 舊 state.json 自動補上新策略（不必重開一輪）")
    old_state = {
        "version": 1, "created": "2026-09-01T00:00:00",
        "initial_capital": 1000000.0,
        "config": {"top_n": 5, "max_position_pct": 0.25, "stop_loss_pct": 0.07,
                   "stop_loss_atr": 2.0, "allow_odd_lot": True,
                   "fee_discount": 0.6, "max_holding_days": 10},
        "portfolios": {n: {"cash": 1000000.0, "positions": {}, "trades": [],
                           "equity_curve": [{"date": "2026-09-01",
                                             "equity": 1000000.0,
                                             "cash": 1000000.0,
                                             "n_positions": 0}],
                           "pending": []}
                       for n in ("strategy_decision", "ev_decision", "score_topn",
                                 "prob_topn", "active_equal", "cash")},
        "processed_dates": ["2026-09-01"], "settled": False,
    }
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "old.json"
        p.write_text(json.dumps(old_state, ensure_ascii=False), encoding="utf-8")
        sim = m.Simulator.load(p)
        check("舊策略的歷史沒有被動到",
              len(sim.portfolios["score_topn"].equity_curve) == 1)
        legacy = {"strategy_decision", "ev_decision", "score_topn",
                  "prob_topn", "active_equal", "cash"}
        check("新策略被補上（舊檔沒有的那些）",
              set(sim.newly_added) == set(m.STRATEGIES) - legacy,
              str(sim.newly_added))
        check("新策略從全額現金起跑",
              all(sim.portfolios[n].cash == 1000000.0 for n in sim.newly_added))
        check("新策略沒有假造歷史（權益曲線是空的）",
              all(sim.portfolios[n].equity_curve == [] for n in sim.newly_added))

        s = sim.summary()
        check("summary 有「起算日」欄位", "起算日" in s.columns)
        new_rows = s[s["策略"].isin(sim.newly_added)]
        check("新策略的起算日標為「尚未起算」",
              (new_rows["起算日"] == "尚未起算").all(),
              str(new_rows[["策略", "起算日"]].to_dict("records")))
        check("舊策略有真正的起算日",
              s[s["策略"] == "score_topn"]["起算日"].iloc[0] == "2026-09-01")

        sim.save(p)
        again = m.Simulator.load(p)
        check("存檔後重讀，策略數不變",
              len(again.portfolios) == len(m.STRATEGIES))
        check("重讀後沒有再被當成新加入",
              again.newly_added == [], str(again.newly_added))


def main():
    print("=" * 64)
    print("  變體策略離線測試")
    print("=" * 64)
    m = load_pt()
    test_registry(m)
    test_same_selection(m)
    test_etf_targets(m)
    test_lowturn_exits(m)
    test_buyhold_never_exits(m)
    test_limit_orders(m)
    test_limit_fill_behaviour(m)
    test_trailing_returns(m)
    test_reversal_strategy(m)
    test_foreign_flow(m)
    test_etf_end_to_end(m)
    test_backward_compat(m)
    print("\n" + "=" * 64)
    print(f"  通過 {_passed} 項 / 失敗 {_failed} 項")
    print("=" * 64)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
