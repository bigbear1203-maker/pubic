# -*- coding: utf-8 -*-
"""
模擬中斷與資料失真的回歸測試
==============================
這幾項都是 2026-10 實測發生過的事，不是假想：

  1. 主資料夾留著舊版 paper_trading.py，流程每次先找到它。舊版不認得
     --trend-file，模擬從 10/12 起每天失敗，整整三週沒有推進——而測試
     跑的是 tools\\ 那份新版，所以全部通過。
  2. 國定假日也推進模擬；抓不到價格的持股用成本價估值。0050 在 9/25（中秋）、
     9/30、10/9（國慶補假）都被打回買進價 113.35，權益曲線出現假的點。
  3. etf_hold 被舊版的單檔 25% 上限壓住，只投入 25%。修好 bug 之後它還是
     停在 25%——買進持有型「已經持有就不再買」。
  4. 漏掉的日子沒有辦法補回來。

執行：python tests/test_recovery.py
"""

import datetime as dt
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

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


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def tool(fname):
    for c in (ROOT / "tools" / fname, ROOT / fname):
        if c.exists():
            return c
    raise FileNotFoundError(fname)


PT = load(tool("paper_trading.py"), "paper_trading")
QUIET = io.StringIO


def write_log(path, rows):
    cols = ["執行時間", "股票代碼", "股價日期(資料基準日)", "預測目標日(隔日估計)",
            "目前股價", "ATR", "綜合分數", "Strategy_Decision", "程式版本"]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df[cols].to_excel(path, index=False, sheet_name="分析紀錄")


# ----------------------------------------------------------------------
def test_valuation_fallback():
    print("\n[1] 取不到價格時，用最近一次收盤價估值（不是成本價）")
    p = PT.Portfolio("x", 1_000_000)
    with redirect_stdout(QUIET()):
        p.execute_buy("0050.TW", 1000, 113.35, dt.date(2026, 9, 22), "t")
    check("買進時記下成交價為最近價", p.positions["0050.TW"].get("last_px") == 113.35)
    p.positions["0050.TW"]["last_px"] = 116.05          # 之後某天看到的收盤
    mv = p.market_value({})                              # 今天抓不到價
    check("抓不到價 → 用最近收盤 116.05 估值，不是成本 113.35",
          abs(mv - 116_050) < 1e-6, f"{mv}")
    old = PT.Portfolio("y", 1_000_000)
    old.positions = {"A.TW": {"shares": 10, "avg_cost": 50.0,
                              "entry_date": "2026-09-01", "stop_price": 46.0}}
    check("舊狀態檔沒有 last_px 時退回成本價（相容）",
          old.market_value({}) == 500.0)


def _two_day_prices(td, syms, d0, d1, missing_on_d1=False):
    rows = []
    for s in syms:
        rows.append({"date": d0.isoformat(), "symbol": s, "open": 100.0, "close": 100.0})
        if not missing_on_d1:
            rows.append({"date": d1.isoformat(), "symbol": s, "open": 101.0, "close": 102.0})
    f = Path(td) / "px.csv"
    pd.DataFrame(rows).to_csv(f, index=False)
    return pd.read_csv(f)


def test_closed_day():
    print("\n[2] 休市日（假日、颱風假）不撮合、不記錄")
    d0, hol = dt.date(2026, 10, 8), dt.date(2026, 10, 9)
    sim = PT.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    p = sim.portfolios["score_topn"]
    with redirect_stdout(QUIET()):
        p.execute_buy("A.TW", 100, 100.0, d0, "t")
    p.pending = [{"symbol": "B.TW", "side": "buy", "reason": "t", "budget": 1e5}]
    sim.state["processed_dates"].append(d0.isoformat())
    n_curve = len(p.equity_curve)
    with tempfile.TemporaryDirectory() as td:
        px = _two_day_prices(td, ["A.TW", "B.TW"], d0, hol, missing_on_d1=True)
        buf = QUIET()
        with redirect_stdout(buf):
            sim.step(hol, None, px)
    check("判定為休市日並說明", "休市日" in buf.getvalue(), buf.getvalue()[-200:])
    check("不寫入已處理日期（之後補跑不會跳過）",
          hol.isoformat() not in sim.state["processed_dates"])
    check("不記錄權益（沒有假的點）", len(p.equity_curve) == n_curve)
    check("委託單保留到下一個交易日", len(p.pending) == 1)
    check("持股沒有被動到", "A.TW" in p.positions)

    sim2 = PT.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    with redirect_stdout(QUIET()):
        sim2.step(d0, None, pd.DataFrame(columns=["date", "symbol", "open", "close"]))
    check("沒有任何持股與委託時（第一天）照常記錄，不誤判成休市",
          d0.isoformat() in sim2.state["processed_dates"])


def test_partial_missing_price():
    print("\n[3] 部分股票抓不到價：其他照常，缺價的用最近收盤")
    d0, d1 = dt.date(2026, 9, 29), dt.date(2026, 9, 30)
    sim = PT.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    p = sim.portfolios["etf_hold"]
    with redirect_stdout(QUIET()):
        p.execute_buy("0050.TW", 2205, 113.35, d0, "t")
        sim.portfolios["score_topn"].execute_buy("A.TW", 100, 100.0, d0, "t")
    p.positions["0050.TW"]["last_px"] = 111.30
    sim.state["processed_dates"].append(d0.isoformat())
    px = pd.DataFrame([{"date": d1.isoformat(), "symbol": "A.TW", "open": 101.0, "close": 102.0}])
    buf = QUIET()
    with redirect_stdout(buf):
        sim.step(d1, None, px)
    check("有價的股票照常處理，這天有被記錄",
          d1.isoformat() in sim.state["processed_dates"])
    check("提示有持股以最近收盤估值", "最近一次收盤價估值" in buf.getvalue())
    eq = p.equity_curve[-1]["equity"]
    exp = p.cash + 2205 * 111.30
    check("0050 以最近收盤 111.30 估值（不是成本 113.35）",
          abs(eq - exp) < 1e-6, f"{eq:.0f} vs {exp:.0f}")


def test_etf_topup():
    print("\n[4] etf_hold 被壓在 25% 時，會把現金補進去")
    sim = PT.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    p = sim.portfolios["etf_hold"]
    with redirect_stdout(QUIET()):
        p.execute_buy("0050.TW", 2205, 113.35, dt.date(2026, 9, 22), "t")
    prices = {"0050.TW": {"open": 116.0, "close": 116.0}}
    before = p.cash / p.equity(prices)
    check(f"重現實際狀況：現金佔 {before:.0%}", before > 0.7)
    orders = sim._build_orders("etf_hold", p, pd.DataFrame(), prices, dt.date(2026, 10, 30))
    buys = [o for o in orders if o["side"] == "buy"]
    check("產生補買單", len(buys) == 1 and buys[0]["symbol"] == "0050.TW", str(orders))
    with redirect_stdout(QUIET()):
        sh = sim._size_position(p, buys[0], 116.0, prices)
        p.execute_buy("0050.TW", sh, 116.0, dt.date(2026, 11, 2), buys[0]["reason"])
    after = p.cash / p.equity(prices)
    check(f"補完後現金只剩 {after:.1%}", after < PT.BUY_HOLD_TOPUP_THRESHOLD, f"{after:.3f}")
    again = sim._build_orders("etf_hold", p, pd.DataFrame(), prices, dt.date(2026, 11, 3))
    check("補滿之後不再重複補買", again == [], str(again))
    check("沒有任何賣單（買進持有）", all(o["side"] != "sell" for o in orders))


def test_missing_dates():
    print("\n[5] 找出漏跑的交易日")
    sim = PT.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    sim.state["processed_dates"] = ["2026-10-07", "2026-10-09"]
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "log.xlsx"
        days = ["2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13",
                "2026-10-14", "2026-10-30"]
        write_log(log, [{"股票代碼": "A.TW", "股價日期(資料基準日)": d,
                         "程式版本": 3.7} for d in days] +
                  [{"股票代碼": "B.TW", "股價日期(資料基準日)": "漲", "程式版本": "漲"}])
        todo = sim.missing_dates(log, dt.date(2026, 10, 30))
        check("只補最後一次處理（10/9）之後的日子",
              [d.isoformat() for d in todo] == ["2026-10-12", "2026-10-13", "2026-10-14"],
              str(todo))
        check("10/8 在最後處理日之前，不回頭改寫歷史",
              dt.date(2026, 10, 8) not in todo)
        check("不含 until 當天（當天由正常的 step 處理）",
              dt.date(2026, 10, 30) not in todo)
        check("錯位列的怪日期被忽略，不會炸", True)
        check("有上限", len(sim.missing_dates(log, dt.date(2026, 10, 30), limit=2)) == 2)
        check("紀錄檔不存在 → 空清單", sim.missing_dates(Path(td) / "無.xlsx",
                                                     dt.date(2026, 10, 30)) == [])


def test_catchup_end_to_end():
    print("\n[6] 端到端：補跑三天，用當天的訊號與當天的趨勢名單")
    days = [dt.date(2026, 10, 12), dt.date(2026, 10, 13), dt.date(2026, 10, 14)]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        rows, px = [], []
        for d in [dt.date(2026, 10, 7)] + days:
            for s in ("A.TW", "B.TW"):
                rows.append({"執行時間": dt.datetime.combine(d, dt.time(15, 30)),
                             "股票代碼": s, "股價日期(資料基準日)": d.isoformat(),
                             "預測目標日(隔日估計)": d.isoformat(), "目前股價": 100.0,
                             "ATR": 2.0, "綜合分數": 5, "Strategy_Decision": "Wait",
                             "程式版本": 3.7})
            for s in ("A.TW", "B.TW", "T.TW", "0050.TW"):
                px.append({"date": d.isoformat(), "symbol": s, "open": 100.0, "close": 100.0})
        log = td / "stock_analysis_log_v3.7.xlsx"
        write_log(log, rows)
        pf = td / "px.csv"
        pd.DataFrame(px).to_csv(pf, index=False)
        trend = pd.DataFrame([{"股票代碼": "T", "排名": 1, "收盤價": 100.0, "ATR": 2.0,
                               "入選": True}])
        with pd.ExcelWriter(td / "趨勢股篩選結果_2026-10-12.xlsx", engine="openpyxl") as w:
            trend.to_excel(w, sheet_name="入選名單", index=False)

        state = td / "s.json"
        with redirect_stdout(QUIET()):
            PT.main(["init", "--state", str(state), "--capital", "1000000"])
            PT.main(["step", "--state", str(state), "--date", "2026-10-07",
                     "--log", str(log), "--offline-prices", str(pf)])
        buf = QUIET()
        with redirect_stdout(buf):
            rc = PT.main(["catchup", "--state", str(state), "--log", str(log),
                          "--until", "2026-10-15", "--trend-dir", str(td),
                          "--offline-prices", str(pf)])
        sim = PT.Simulator.load(state)
        done = sim.state["processed_dates"]
        check("執行成功", rc == 0)
        check("三天都補上了", all(d.isoformat() in done for d in days), str(done))
        check("說明補跑用的是當天的資料", "不會用到當天之後才知道的資訊" in buf.getvalue())
        tp = sim.portfolios["trend_persistent"]
        check("補跑時有讀到當天的趨勢名單（trend_persistent 有動作）",
              sim.state.get("last_rebalance", {}).get("trend_persistent") == "2026-10",
              str(sim.state.get("last_rebalance")))
        check("趨勢名單的股票隔天成交", "T.TW" in tp.positions, str(list(tp.positions)))
        buf = QUIET()
        with redirect_stdout(buf):
            PT.main(["catchup", "--state", str(state), "--log", str(log),
                     "--until", "2026-10-15", "--offline-prices", str(pf)])
        check("再跑一次 → 沒有漏掉的日子，不重複處理", "沒有漏掉" in buf.getvalue())


def test_forget_dates():
    print("\n[10] 移除誤記的休市日")
    sim = PT.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    p = sim.portfolios["score_topn"]
    for d in ("2026-10-07", "2026-10-09"):
        sim.state["processed_dates"].append(d)
        for q in sim.portfolios.values():
            q.equity_curve.append({"date": d, "equity": 1e6, "cash": 1e6, "n_positions": 0})
    sim.state["processed_dates"].append("2026-10-12")
    p.trades.append({"date": "2026-10-12", "symbol": "A.TW", "side": "買進"})
    removed, refused = sim.forget_dates(["2026-10-09", "2026-10-12", "2026-09-01"])
    check("沒有成交的 10/9 被移除", removed == ["2026-10-09"], str(removed))
    check("有成交的 10/12 拒絕移除", refused == ["2026-10-12"], str(refused))
    check("所有策略的權益曲線都拿掉 10/9",
          all(all(e["date"] != "2026-10-09" for e in q.equity_curve)
              for q in sim.portfolios.values()))
    check("已處理日期也拿掉 10/9", "2026-10-09" not in sim.state["processed_dates"])
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "log.xlsx"
        write_log(log, [{"股票代碼": "A.TW", "股價日期(資料基準日)": d, "程式版本": 3.7}
                        for d in ("2026-10-07", "2026-10-08", "2026-10-13")])
        sim.state["processed_dates"].remove("2026-10-12")
        todo = sim.missing_dates(log, dt.date(2026, 10, 30))
        check("移除 10/9 之後，10/8 變成可以補跑",
              dt.date(2026, 10, 8) in todo, str(todo))


def _make_tree(td, main_src, tools_src, main_mtime_older=True):
    root = Path(td) / "股票分析"
    (root / "tools").mkdir(parents=True)
    (root / "paper_trading.py").write_text(main_src, encoding="utf-8")
    (root / "tools" / "paper_trading.py").write_text(tools_src, encoding="utf-8")
    old = time.time() - 86400 * 20
    if main_mtime_older:
        os.utime(root / "paper_trading.py", (old, old))
    else:
        os.utime(root / "tools" / "paper_trading.py", (old, old))
    return root


def test_pipeline_duplicates():
    print("\n[7] 每日流程：同一支程式有兩份且內容不同")
    pl = load(ROOT / "tw_stock_pipeline_v1.1.py", "pipeline_dup")
    with tempfile.TemporaryDirectory() as td:
        root = _make_tree(td, "# 舊版（9/19）\n", "# 新版（10/10）\n")
        pl.SCRIPT_DIR = root
        pl.DUPLICATE_WARNINGS.clear()
        chosen = pl._find("paper_trading.py")
        check("重現實際狀況：選到 tools\\ 的新版，而不是主資料夾的舊版",
              chosen == root / "tools" / "paper_trading.py", str(chosen))
        check("記下警告並指出該移走哪一份",
              len(pl.DUPLICATE_WARNINGS) == 1 and "paper_trading.py" in pl.DUPLICATE_WARNINGS[0]
              and "舊版" in pl.DUPLICATE_WARNINGS[0], str(pl.DUPLICATE_WARNINGS))

    with tempfile.TemporaryDirectory() as td:
        root = _make_tree(td, "# 新\n", "# 舊\n", main_mtime_older=False)
        pl.SCRIPT_DIR = root
        pl.DUPLICATE_WARNINGS.clear()
        check("反過來（主資料夾比較新）也選最新的那份",
              pl._find("paper_trading.py") == root / "paper_trading.py")

    with tempfile.TemporaryDirectory() as td:
        root = _make_tree(td, "# 一樣\n", "# 一樣\n")
        pl.SCRIPT_DIR = root
        pl.DUPLICATE_WARNINGS.clear()
        pl._find("paper_trading.py")
        check("兩份內容相同 → 不警告", pl.DUPLICATE_WARNINGS == [])

    src = (ROOT / "tw_stock_pipeline_v1.1.py").read_text(encoding="utf-8")
    check("啟動時印出重複檔警告", "同一支程式有好幾份" in src)
    check("模擬之前先補跑漏掉的日子", '"catchup"' in src)
    check("結束時列出每個步驟成功或失敗", "各步驟結果" in src)

    pl.STEP_RESULTS.clear()
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "boom.py"
        bad.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
        with redirect_stdout(QUIET()):
            pl._run_tool(bad, [], "紙上交易模擬")
    check("失敗的步驟會被記下來（不再只是中間一行字）",
          pl.STEP_RESULTS == [("紙上交易模擬", False)], str(pl.STEP_RESULTS))


def test_stock_py_duplicates():
    print("\n[8] stock.py：同樣的保護")
    sp = load(ROOT / "stock.py", "stock_dup")
    with tempfile.TemporaryDirectory() as td:
        root = _make_tree(td, "# 舊\n", "# 新\n")
        sp.ROOT = root
        with redirect_stdout(QUIET()):
            got = sp.find("paper_trading.py")
        check("find() 選最新的那份", got == root / "tools" / "paper_trading.py", str(got))
        check("copies_differ 能分辨", sp.copies_differ(sp.find_all("paper_trading.py")))


def test_event_stats_cleaning():
    print("\n[9] events --stats：排除錯位列、同日重複只算一次")
    ec = load(tool("event_calendar.py"), "event_calendar_rec")
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "log.xlsx"
        rows = []
        for i in range(40):
            d = (dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat()
            rows.append({"執行時間": f"{d} 15:30", "股票代碼": "A.TW",
                         "股價日期(資料基準日)": d, "預測目標日(隔日估計)": d,
                         "實際報酬(%)": 1.0, "程式版本": 3.7})
        rows.append({"執行時間": "2026-09-01 10:00", "股票代碼": "B.TW",
                     "股價日期(資料基準日)": "2026-08-31", "預測目標日(隔日估計)": "2026-09-01",
                     "實際報酬(%)": 1.0, "程式版本": "漲"})
        rows.append({**rows[0], "執行時間": "2026-09-01 09:00"})
        pd.DataFrame(rows).to_excel(log, index=False, sheet_name="分析紀錄")
        buf = QUIET()
        with redirect_stdout(buf):
            ec.event_stats(log, ec.TradingCalendar())
        out = buf.getvalue()
        check("排除 1 筆錯位列", "排除欄位錯位的 1 筆" in out, out[:300])
        check("重複執行只算一次", "重複執行的 1 筆只算一次" in out, out[:300])


def main():
    print("=" * 64)
    print("  模擬中斷與資料失真 回歸測試")
    print("=" * 64)
    # 每一段各自執行：某一段崩潰只記為失敗，不要把後面的檢查一起蓋掉
    for fn in (test_valuation_fallback, test_closed_day, test_partial_missing_price,
               test_etf_topup, test_missing_dates, test_catchup_end_to_end,
               test_pipeline_duplicates, test_stock_py_duplicates,
               test_event_stats_cleaning, test_forget_dates):
        try:
            fn()
        except (Exception, SystemExit) as e:                       # noqa: BLE001
            # SystemExit 也要接住：舊版程式遇到不認得的參數時 argparse 會直接
            # 結束整個行程——那正是實際讓模擬停擺三週的方式。
            check(f"{fn.__name__} 執行完畢", False, f"{type(e).__name__}: {e}")
    print("\n" + "=" * 64)
    print(f"  通過 {_passed} 項 / 失敗 {_failed} 項")
    print("=" * 64)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
