# -*- coding: utf-8 -*-
"""
持續上漲股篩選 + trend_persistent 策略 離線測試
=================================================
不連網。三件事最重要：

  1. 指標只看過去，不碰未來。
  2. 每一種落選原因都能被單獨觸發——否則「為什麼沒選這檔」的說明不可信。
  3. 每月才調整一次。這是文獻結果成立的前提；每天換股會被成本吃光。

執行：python tests/test_trend.py
"""

import datetime as dt
import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stdout
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
for _p in (ROOT, ROOT / "tools"):
    sys.path.insert(0, str(_p))

import trend_screener as ts                                      # noqa: E402

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def load(fname, modname):
    for cand in (ROOT / "tools" / fname, ROOT / fname):
        if cand.exists():
            spec = importlib.util.spec_from_file_location(modname, cand)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise FileNotFoundError(fname)


N = ts.HISTORY_DAYS_NEEDED + 5
IDX = pd.bdate_range("2025-01-01", periods=N)


def series_from_daily(daily):
    return pd.Series(100 * np.cumprod(1 + np.asarray(daily, dtype=float)), index=IDX)


# ----------------------------------------------------------------------
def test_metrics_windows():
    print("\n[1] 指標的時間窗口")
    # 每天固定 +0.1%：所有報酬都可以手算
    r = 0.001
    c = series_from_daily(np.full(N, r))
    m = ts.compute_metrics(c)
    exp_form = (1 + r) ** ts.FORMATION_DAYS - 1
    exp_recent = (1 + r) ** ts.SKIP_DAYS - 1
    check("形成期報酬 = 126 天的複利",
          abs(m["形成期報酬(%)"] / 100 - exp_form) < 1e-9,
          f"{m['形成期報酬(%)']:.6f} vs {exp_form * 100:.6f}")
    check("前一期報酬 = 同樣長度的前一段",
          abs(m["前一期報酬(%)"] / 100 - exp_form) < 1e-9)
    check("最近一個月 = 被跳過的 21 天",
          abs(m["最近一個月(%)"] / 100 - exp_recent) < 1e-9)
    check("天天漲 → 上漲日 100%", abs(m["上漲日比例(%)"] - 100) < 1e-9)
    check("天天漲 → ID = -1（最連續）", abs(m["連續性ID"] - (-1.0)) < 1e-9,
          str(m["連續性ID"]))

    check("歷史不足時回傳 None", ts.compute_metrics(c.tail(100)) is None)
    check("剛好夠長時可以算",
          ts.compute_metrics(c.tail(ts.HISTORY_DAYS_NEEDED)) is not None)

    # 不碰未來：形成期只到 t-21，所以最後 21 天怎麼變都不影響形成期報酬
    d = np.full(N, r)
    d[-21:] = 0.05
    m2 = ts.compute_metrics(series_from_daily(d))
    check("最後 21 天暴漲不改變形成期報酬（跳過的月份真的被跳過）",
          abs(m2["形成期報酬(%)"] - m["形成期報酬(%)"]) < 1e-9)
    check("但會反映在「最近一個月」",
          m2["最近一個月(%)"] > m["最近一個月(%)"] + 100)


def test_id_sign():
    print("\n[2] 連續性 ID：同樣漲幅，天天小漲 vs 幾天大漲")
    n_form = ts.FORMATION_DAYS
    # 兩條路徑，形成期的累積報酬相同（都約 +30%）
    smooth = np.full(N, (1.30) ** (1 / n_form) - 1)
    jumpy = np.full(N, -0.0005)
    form_slice = slice(N - 1 - ts.SKIP_DAYS - n_form + 1, N - ts.SKIP_DAYS)
    k = 6
    base = (1 - 0.0005) ** (n_form - k)
    jump = (1.30 / base) ** (1 / k) - 1
    positions = np.arange(form_slice.start, form_slice.stop)[:: n_form // k][:k]
    jumpy[positions] = jump
    ms = ts.compute_metrics(series_from_daily(smooth))
    mj = ts.compute_metrics(series_from_daily(jumpy))
    check("兩條路徑形成期報酬相近（±3 個百分點）",
          abs(ms["形成期報酬(%)"] - mj["形成期報酬(%)"]) < 3,
          f"{ms['形成期報酬(%)']:.1f} vs {mj['形成期報酬(%)']:.1f}")
    check("天天小漲的 ID 為負", ms["連續性ID"] < 0, str(ms["連續性ID"]))
    check("幾天大漲的 ID 為正", mj["連續性ID"] > 0, str(mj["連續性ID"]))

    down = ts.compute_metrics(series_from_daily(np.full(N, -0.001)))
    check("天天跌：sign 為負，ID = sign × (跌日 − 漲日) = -1 × (1 − 0) = -1",
          abs(down["連續性ID"] - (-1.0)) < 1e-9, str(down["連續性ID"]))
    check("所以 ID < 0 本身不代表上漲——必須先過「贏家」那關",
          down["形成期報酬(%)"] < 0)


def test_volume_and_atr():
    print("\n[3] 成交額與 ATR")
    c = series_from_daily(np.full(N, 0.001))
    vol = pd.Series(1000.0, index=IDX)
    m = ts.compute_metrics(c, vol, c * 1.02, c * 0.98)
    exp_turnover = (c * 1000).tail(20).mean()
    check("20 日均成交額 = 收盤價 × 量 的 20 日平均",
          abs(m["20日均成交額"] - exp_turnover) < 1e-6)
    check("ATR 為正", m["ATR"] > 0)
    m2 = ts.compute_metrics(c)
    check("沒有量時成交額為 NaN（不是 0）", np.isnan(m2["20日均成交額"]))


# ----------------------------------------------------------------------
def make_pool(extra_rows):
    """20 檔普通股撐起門檻，再加上要測的那幾檔（形成期報酬都很高）。"""
    rng = np.random.default_rng(1)
    rows = []
    for i in range(20):
        rows.append({"股票代碼": f"F{i:02d}", "股票名稱": "",
                     "形成期報酬(%)": float(rng.uniform(-20, 10)),
                     "前一期報酬(%)": 5.0, "最近一個月(%)": 2.0,
                     "上漲日比例(%)": 50.0, "連續性ID": 0.0,
                     "20日均成交額": 1e8, "ATR": 1.0, "收盤價": 100.0,
                     "站上60日均線": True})
    base = {"股票名稱": "", "前一期報酬(%)": 20.0, "最近一個月(%)": 5.0,
            "上漲日比例(%)": 70.0, "連續性ID": -0.4, "20日均成交額": 1e8,
            "ATR": 2.0, "收盤價": 100.0, "站上60日均線": True}
    for r in extra_rows:
        rows.append({**base, **r})
    return pd.DataFrame(rows)


def test_each_rejection_isolated():
    print("\n[4] 每一種落選原因都能被單獨觸發")
    pool = make_pool([
        {"股票代碼": "GOOD", "形成期報酬(%)": 40.0},
        {"股票代碼": "ILLIQ", "形成期報酬(%)": 45.0, "20日均成交額": 1e5},
        {"股票代碼": "NOTPERSIST", "形成期報酬(%)": 50.0, "前一期報酬(%)": -10.0},
        {"股票代碼": "JUMPY", "形成期報酬(%)": 55.0, "連續性ID": 0.2},
        {"股票代碼": "SPIKE", "形成期報酬(%)": 60.0, "最近一個月(%)": 40.0},
        {"股票代碼": "NOHIST", "形成期報酬(%)": np.nan, "前一期報酬(%)": np.nan},
    ])
    res = ts.rank_universe(pool, top_n=5).set_index("股票代碼")
    expect = {
        "GOOD": None,
        "ILLIQ": ts.REJECT_LIQUIDITY,
        "NOTPERSIST": ts.REJECT_NOT_PERSISTENT,
        "JUMPY": ts.REJECT_DISCRETE,
        "SPIKE": ts.REJECT_SPIKE,
        "NOHIST": ts.REJECT_HISTORY,
    }
    for sym, reason in expect.items():
        row = res.loc[sym]
        if reason is None:
            check(f"{sym} 入選", bool(row["入選"]), row["未入選原因"])
        else:
            check(f"{sym} 落選原因 = {reason}",
                  (not row["入選"]) and row["未入選原因"] == reason,
                  f"實際：{row['未入選原因']!r}")
    weak = res.loc["F00"]
    check("一般股以「漲幅不在前段」落選",
          weak["未入選原因"] == ts.REJECT_NOT_WINNER, weak["未入選原因"])


def test_ranking_rules():
    print("\n[5] 排名規則")
    pool = make_pool([
        {"股票代碼": "A", "形成期報酬(%)": 30.0, "連續性ID": -0.2},
        {"股票代碼": "B", "形成期報酬(%)": 25.0, "連續性ID": -0.6},
        {"股票代碼": "C", "形成期報酬(%)": 35.0, "連續性ID": -0.4},
    ])
    res = ts.rank_universe(pool, top_n=5)
    chosen = res[res["入選"]]["股票代碼"].tolist()
    check("依 ID 由低到高排（越連續越前面），不是依漲幅",
          chosen == ["B", "C", "A"], str(chosen))

    res2 = ts.rank_universe(pool, top_n=2)
    chosen2 = res2[res2["入選"]]["股票代碼"].tolist()
    check("top_n=2 只取前兩名", chosen2 == ["B", "C"], str(chosen2))
    a = res2.set_index("股票代碼").loc["A"]
    check("第三名標示為「通過但排在後面」，不是當成沒通過",
          "通過所有條件" in a["未入選原因"], a["未入選原因"])

    only_one = make_pool([{"股票代碼": "SOLO", "形成期報酬(%)": 40.0}])
    r1 = ts.rank_universe(only_one, top_n=5)
    check("top_n 是上限不是配額：只有 1 檔合格就只選 1 檔",
          int(r1["入選"].sum()) == 1, str(int(r1["入選"].sum())))

    nan_turn = make_pool([{"股票代碼": "NT", "形成期報酬(%)": 40.0,
                           "20日均成交額": np.nan}])
    r2 = ts.rank_universe(nan_turn, top_n=5).set_index("股票代碼")
    check("成交額未知（NaN）不當作流動性不足——那是資料問題不是股票問題",
          bool(r2.loc["NT", "入選"]), r2.loc["NT", "未入選原因"])

    empty = ts.rank_universe(pd.DataFrame(columns=pool.columns), top_n=5)
    check("空的股票池不會炸", empty.empty)

    winners_pool_only = make_pool([
        {"股票代碼": "HI", "形成期報酬(%)": 40.0},
        {"股票代碼": "ILLIQ_HUGE", "形成期報酬(%)": 500.0, "20日均成交額": 1},
    ])
    r3 = ts.rank_universe(winners_pool_only, top_n=5).set_index("股票代碼")
    check("前 30% 只在流動股裡算——冷門股的極端漲幅不會把門檻拉走",
          bool(r3.loc["HI", "入選"]), r3.loc["HI", "未入選原因"])


def test_screen_end_to_end():
    print("\n[6] screen()：從歷史資料到排名")
    rng = np.random.default_rng(3)
    hist = {}

    def mk(daily, vol=1e6):
        c = series_from_daily(daily)
        return {"Close": c, "Volume": pd.Series(vol, index=IDX),
                "High": c * 1.01, "Low": c * 0.99}

    for i in range(3):
        hist[f"S{i}"] = mk(rng.normal(0.003 + i * 0.0003, 0.004, N))
    for i in range(15):
        hist[f"F{i}"] = mk(rng.normal(0.0, 0.006, N))
    hist["SHORT"] = {k: v.tail(50) for k, v in mk(np.full(N, 0.001)).items()}

    res = ts.screen(hist, names={"S0": "穩漲零"}, top_n=5)
    chosen = set(res[res["入選"]]["股票代碼"])
    check("三檔穩定上漲股全部入選", {"S0", "S1", "S2"} <= chosen, str(chosen))

    ranked = res[res["入選"]].sort_values("排名")["股票代碼"].tolist()
    check("真正的趨勢股排在最前面（第 1~3 名）",
          set(ranked[:3]) == {"S0", "S1", "S2"}, str(ranked))

    # 已知限制，刻意寫成測試而不是修掉：
    # F 開頭的是期望報酬為零的隨機漫步，但有幾檔「純靠運氣」在兩個期間
    # 都漲、上漲日也多於下跌日，跟真正的持續上漲在規則上無法區分。
    # 它們會排在真趨勢後面，但 top_n 有空位時仍會被選進來。
    # 不加「ID ≤ 某個值」的門檻來擋——那是拿自己造的假資料調參數。
    # 這正是為什麼名單好不好要靠紙上交易去驗證，不能靠篩選規則本身保證。
    lucky = [c for c in ranked if c.startswith("F")]
    if lucky:
        f_ids = res.set_index("股票代碼").loc[lucky, "連續性ID"]
        s_ids = res.set_index("股票代碼").loc[["S0", "S1", "S2"], "連續性ID"]
        check(f"混進來的隨機漫步（{lucky}）連續性明顯弱於真趨勢",
              f_ids.min() > s_ids.max(), f"F {f_ids.tolist()} vs S {s_ids.tolist()}")
    else:
        check("（這組亂數沒有隨機漫步混進來）", True)
    short = res.set_index("股票代碼").loc["SHORT"]
    check("歷史太短的標成「資料不足」", short["未入選原因"] == ts.REJECT_HISTORY)
    check("名稱有帶進結果",
          res.set_index("股票代碼").loc["S0", "股票名稱"] == "穩漲零")
    check("screen({}) 回傳空表", ts.screen({}).empty)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "趨勢股篩選結果_test.xlsx"
        ts.save(res, out)
        # 一定要用 with 關檔：Windows 不能刪除還開著的檔案，
        # 暫存資料夾清理時會 PermissionError（Linux 上測不出來）。
        with pd.ExcelFile(out) as xf:
            sheets = xf.sheet_names
        check("輸出含「入選名單」與「全部候選」兩張表",
              "入選名單" in sheets and "全部候選與未入選原因" in sheets, str(sheets))
        back = pd.read_excel(out, sheet_name="入選名單")
        check("入選名單只有入選的股票", len(back) == len(chosen))

    buf = io.StringIO()
    with redirect_stdout(buf):
        ts.print_result(res, 5)
    check("列印結果含落選原因分布", "落選原因分布" in buf.getvalue())

    none_pass = res.copy()
    none_pass["入選"] = False
    buf = io.StringIO()
    with redirect_stdout(buf):
        ts.print_result(none_pass, 5)
    check("沒有任何入選時明講「不是錯誤」",
          "不是錯誤" in buf.getvalue(), buf.getvalue()[:120])


def test_prefilter():
    print("\n[7] 下載前的成交額預篩")
    u = pd.DataFrame({"stock_id": [f"{i}" for i in range(10)],
                      "stock_name": [""] * 10,
                      "latest_trading_money": list(range(10))})
    top = ts.prefilter(u, n=3)
    check("依成交額由高到低取前 n", top["stock_id"].tolist() == ["9", "8", "7"],
          str(top["stock_id"].tolist()))
    no_col = ts.prefilter(u.drop(columns=["latest_trading_money"]), n=3)
    check("沒有成交額欄位時退回取前 n 筆", len(no_col) == 3)


# ----------------------------------------------------------------------
def write_trend_file(path, symbols, atr=2.0, close=100.0):
    df = pd.DataFrame([{"股票代碼": s, "股票名稱": "", "排名": i + 1,
                        "收盤價": close, "ATR": atr, "入選": True}
                       for i, s in enumerate(symbols)])
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="入選名單", index=False)
        df.to_excel(w, sheet_name="全部候選與未入選原因", index=False)


def test_load_trend_file(pt):
    print("\n[8] 模擬器讀趨勢名單")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.xlsx"
        write_trend_file(p, [2330, "2454", "3008.TW"])
        t = pt.load_trend_file(p)
        check("數字代碼補上 .TW（Excel 讀成 2330 或 2330.0 都要對）",
              t["股票代碼"].tolist() == ["2330.TW", "2454.TW", "3008.TW"],
              str(t["股票代碼"].tolist()))
        check("依排名排序", t["排名"].tolist() == [1, 2, 3])
        check("帶出 ATR 與收盤價", {"ATR", "收盤價"} <= set(t.columns))

        check("檔案不存在 → 空表", pt.load_trend_file(Path(td) / "無.xlsx").empty)
        check("路徑是 None → 空表", pt.load_trend_file(None).empty)

        bad = Path(td) / "bad.xlsx"
        pd.DataFrame({"x": [1]}).to_excel(bad, sheet_name="別的表", index=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            r = pt.load_trend_file(bad)
        check("沒有「入選名單」工作表 → 空表且有提示",
              r.empty and "讀不到趨勢名單" in buf.getvalue())

        empty = Path(td) / "empty.xlsx"
        write_trend_file(empty, [])
        check("入選名單是空的 → 空表", pt.load_trend_file(empty).empty)


def test_pick_and_gate(pt):
    print("\n[9] 選股與每月調整閘門")
    trend = pd.DataFrame({"股票代碼": ["A.TW", "B.TW", "C.TW"],
                          "排名": [1, 2, 3], "收盤價": [100.0] * 3, "ATR": [2.0] * 3})
    got = pt.pick_targets("trend_persistent", pd.DataFrame(), 2, trend=trend)
    check("訊號表是空的也能從趨勢名單選", [g[0] for g in got] == ["A.TW", "B.TW"],
          str(got))
    check("沒有名單時不選", pt.pick_targets("trend_persistent", pd.DataFrame(), 2) == [])
    check("其他策略不受 trend 參數影響",
          pt.pick_targets("cash", pd.DataFrame(), 2, trend=trend) == [])

    sim = pt.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    d1 = dt.date(2026, 10, 12)
    check("第一次（本月尚未調整）→ 調整日",
          sim.is_rebalance_day("trend_persistent", d1, trend))
    p = sim.portfolios["trend_persistent"]
    orders = sim._build_orders("trend_persistent", p, pd.DataFrame(), {}, d1,
                               trend=trend)
    check("調整日產生買單", any(o["side"] == "buy" for o in orders), str(orders))
    check("記下本月已調整",
          sim.state.get("last_rebalance", {}).get("trend_persistent") == "2026-10")

    d2 = dt.date(2026, 10, 20)
    check("同月第二次 → 不是調整日",
          not sim.is_rebalance_day("trend_persistent", d2, trend))
    check("非調整日不產生任何委託",
          sim._build_orders("trend_persistent", p, pd.DataFrame(), {}, d2,
                            trend=trend) == [])

    d3 = dt.date(2026, 11, 2)
    check("下個月 → 調整日", sim.is_rebalance_day("trend_persistent", d3, trend))

    sim2 = pt.Simulator.create(1_000_000, 5, 0.25, 0.07, 2.0, True, 0.6)
    check("沒有名單 → 不是調整日",
          not sim2.is_rebalance_day("trend_persistent", d1, pd.DataFrame()))
    sim2._build_orders("trend_persistent", sim2.portfolios["trend_persistent"],
                       pd.DataFrame(), {}, d1, trend=pd.DataFrame())
    check("沒有名單時不記錄為已調整（明天有名單還能補做）",
          "trend_persistent" not in sim2.state.get("last_rebalance", {}))
    check("其他策略永遠視為調整日（閘門只管每月型）",
          sim2.is_rebalance_day("score_topn", d1, pd.DataFrame()))

    buys = [o for o in orders if o["side"] == "buy"]
    check("買單帶有名單裡的 ATR（停損不必退回固定百分比）",
          all(o.get("atr") == 2.0 for o in buys), str(buys[:1]))


def test_exits(pt):
    print("\n[10] 出場規則")
    trend = pd.DataFrame({"股票代碼": ["A.TW", "B.TW"], "排名": [1, 2],
                          "收盤價": [100.0, 100.0], "ATR": [2.0, 2.0]})
    old = {"OLD.TW": {"shares": 100, "avg_cost": 100.0,
                      "entry_date": "2026-01-01", "stop_price": 93.0},
           "A.TW": {"shares": 100, "avg_cost": 100.0,
                    "entry_date": "2026-01-01", "stop_price": 93.0}}
    targets = pt.pick_targets("trend_persistent", pd.DataFrame(), 5, trend=trend)
    ex = pt.pick_exits("trend_persistent", pd.DataFrame(), old, targets, 10,
                       dt.date(2026, 10, 12))
    check("持有超過 10 天但仍在名單上的 A 不賣（不吃時間停損）",
          "A.TW" not in ex, str(ex))
    check("掉出名單的 OLD 在調整日賣出", "OLD.TW" in ex, str(ex))

    ex2 = pt.pick_exits("score_topn", pd.DataFrame({"股票代碼": ["A.TW"],
                                                    "綜合分數": [5]}),
                        {"A.TW": old["A.TW"]}, [("A.TW", "")], 10,
                        dt.date(2026, 10, 12))
    check("對照：score_topn 同樣情況會被時間停損",
          "A.TW" in ex2 and "時間停損" in ex2["A.TW"], str(ex2))


def test_end_to_end(pt):
    print("\n[11] 端到端：跨月份，只在每月第一次有名單時換股")
    days = [d for d in (dt.date(2026, 10, 26) + dt.timedelta(days=i)
                        for i in range(14)) if d.weekday() < 5]
    syms = ["A.TW", "B.TW", "C.TW"]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        px = []
        for d in [days[0] - dt.timedelta(days=3)] + days:
            for s in syms:
                px.append({"date": d.isoformat(), "symbol": s,
                           "open": 100.0, "close": 100.0})
        pf = td / "px.csv"
        pd.DataFrame(px).to_csv(pf, index=False)

        oct_file = td / "oct.xlsx"
        nov_file = td / "nov.xlsx"
        write_trend_file(oct_file, ["A", "B"])
        write_trend_file(nov_file, ["B", "C"])      # 11 月 A 掉出、C 進來

        state = td / "s.json"
        with redirect_stdout(io.StringIO()):
            pt.main(["init", "--state", str(state), "--capital", "1000000",
                     "--top-n", "2"])
            for d in days:
                f = oct_file if d.month == 10 else nov_file
                pt.main(["step", "--state", str(state), "--date", d.isoformat(),
                         "--trend-file", str(f), "--offline-prices", str(pf)])
        sim = pt.Simulator.load(state)
        tp = sim.portfolios["trend_persistent"]
        trade_days = sorted({t["date"] for t in tp.trades})
        check("有成交", len(tp.trades) > 0, str(tp.trades))
        check("只在兩個日子有成交（10 月調整的隔天、11 月調整的隔天）",
              len(trade_days) == 2, str(trade_days))
        check("最後持股是 11 月名單 B、C", sorted(tp.positions) == ["B.TW", "C.TW"],
              str(sorted(tp.positions)))
        sold = [t["symbol"] for t in tp.trades if t["side"] == "賣出"]
        check("A 在 11 月被換掉", sold == ["A.TW"], str(sold))
        check("B 從頭到尾沒被賣（還在名單上就不動）", "B.TW" not in sold)
        check("沒有活躍股紀錄檔也能運作（選股池獨立）",
              sim.state["last_rebalance"]["trend_persistent"] == "2026-11")


def test_pipeline_resilience():
    print("\n[12] 每日流程：篩選失敗不中斷整條流程")
    pl = load("tw_stock_pipeline_v1.1.py", "pipeline_under_test")
    check("流程裡有 Step 1b", hasattr(pl, "run_trend_screen"))
    src = (ROOT / "tw_stock_pipeline_v1.1.py").read_text(encoding="utf-8")
    check("有把 --trend-file 傳給模擬器", '"--trend-file"' in src)

    with tempfile.TemporaryDirectory() as td:
        boom = Path(td) / "trend_screener.py"
        boom.write_text("def prefilter(u):\n    raise RuntimeError('網路斷了')\n",
                        encoding="utf-8")
        pl.TREND_SCREENER = boom
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = pl.run_trend_screen(pd.DataFrame({"stock_id": ["1"]}),
                                      dt.date(2026, 10, 12))
        check("篩選器丟例外 → 回傳 None，不往外拋", out is None)
        check("並且明講其他步驟照常", "其他步驟照常" in buf.getvalue(),
              buf.getvalue()[-150:])

        pl.TREND_SCREENER = Path(td) / "不存在.py"
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = pl.run_trend_screen(pd.DataFrame(), dt.date(2026, 10, 12))
        check("找不到篩選器 → 回傳 None 並說明", out is None and "略過" in buf.getvalue())


def test_wiring():
    print("\n[13] 接線")
    stock_py = ROOT / "stock.py"
    src = stock_py.read_text(encoding="utf-8") if stock_py.exists() else ""
    hint = f"檢查的是 {stock_py}，請把新版 stock.py 覆蓋到這裡"
    check("stock.py 有 trend 指令", 'cmd == "trend"' in src, hint)
    check("trend_screener.py 列在工具清單", '"trend_screener.py"' in src, hint)

    # 設定檔的「趨勢股篩選」區塊是選用的：沒貼的話篩選器要退回內建預設值。
    # 所以兩種情況都要能過，不能把「沒貼」當成錯誤，更不能因此崩潰。
    import json
    cfg_path = ROOT / "stock_settings.json"
    cfg = (json.loads(cfg_path.read_text(encoding="utf-8"))
           if cfg_path.exists() else {})
    section = cfg.get("趨勢股篩選")
    if section is None:
        check("設定檔沒有「趨勢股篩選」區塊 → 使用內建預設值（21 / 126 / 30% / 5 檔）",
              (ts.SKIP_DAYS, ts.FORMATION_DAYS, ts.TOP_FRACTION, ts.TOP_N)
              == (21, 126, 0.30, 5),
              f"{(ts.SKIP_DAYS, ts.FORMATION_DAYS, ts.TOP_FRACTION, ts.TOP_N)}")
    else:
        check("設定檔有「趨勢股篩選」區塊，且數值有被篩選器讀到",
              ts.TOP_N == int(section.get("top_n", ts.TOP_N))
              and ts.SKIP_DAYS == int(section.get("skip_days", ts.SKIP_DAYS)),
              f"設定檔 top_n={section.get('top_n')}，篩選器讀到 {ts.TOP_N}")


def main():
    print("=" * 64)
    print("  持續上漲股篩選 離線測試")
    print("=" * 64)
    test_metrics_windows()
    test_id_sign()
    test_volume_and_atr()
    test_each_rejection_isolated()
    test_ranking_rules()
    test_screen_end_to_end()
    test_prefilter()
    pt = load("paper_trading.py", "paper_trading")
    test_load_trend_file(pt)
    test_pick_and_gate(pt)
    test_exits(pt)
    test_end_to_end(pt)
    test_pipeline_resilience()
    test_wiring()
    print("\n" + "=" * 64)
    print(f"  通過 {_passed} 項 / 失敗 {_failed} 項")
    print("=" * 64)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
