# -*- coding: utf-8 -*-
"""
獲利能力評估 離線測試
======================
不連網。重點在「判斷標準有沒有被正確套用」，以及「資料不完整時會不會
給出看起來像結論的東西」——後者比前者危險：一個沉默的錯誤結論會讓人
拿真錢下去。

執行：python tests/test_evaluate.py
"""

import ast
import datetime as dt
import importlib.util
import io
import json
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

import evaluate as ev                                            # noqa: E402

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
    spec = importlib.util.spec_from_file_location(
        "paper_trading", ROOT / "tools" / "paper_trading.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def analyzer_columns():
    src = sorted(ROOT.glob("claude_stock_analyzer_v*.py"))[-1]
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == "EXCEL_LOG_COLUMNS" for t in n.targets):
            return [ast.literal_eval(e) for e in n.value.elts]
    return None


# ----------------------------------------------------------------------
def test_wilson():
    print("\n[1] Wilson 下界公式取自分析程式，不自己複製一份")
    f, src = ev.get_wilson()
    check("成功取得公式", callable(f), str(src))
    check("來源是分析程式而非本地備援",
          src.startswith("claude_stock_analyzer"), src)

    # 跟本地備援比對，兩者必須一致——不一致代表其中一份走鐘了
    for acc, n in [(55, 100), (52, 40), (70, 74), (50, 200), (60, 30)]:
        a, b = f(acc, n), ev._wilson_fallback(acc, n)
        check(f"公式一致 acc={acc}% n={n}（{a:.2f} vs {b:.2f}）",
              abs(a - b) < 1e-6, f"{a} vs {b}")

    check("樣本數 0 回傳 None", f(55, 0) is None)
    # 準確率 74%、樣本 40 → 下界仍在 50% 以上（先前 2891C 的實例）
    check("acc=74% n=40 的下界仍 > 50%（小樣本不必然被擋）",
          f(74, 40) > 50, f"{f(74, 40):.1f}")
    check("acc=52% n=200 的下界 < 50%（接近丟銅板會被擋）",
          f(52, 200) < 50, f"{f(52, 200):.1f}")


def test_mean_ci():
    print("\n[2] 信賴區間")
    m, lo, hi, n = ev.mean_ci([1, 2, 3, 4, 5])
    check("平均正確", abs(m - 3.0) < 1e-9)
    check("區間包住平均", lo < m < hi)
    check("n 正確", n == 5)
    m, lo, hi, n = ev.mean_ci([])
    check("空資料不炸，回傳 n=0", n == 0 and np.isnan(m))
    m, lo, hi, n = ev.mean_ci([7.0])
    check("單筆資料有平均但無區間", m == 7.0 and np.isnan(lo) and n == 1)
    m, lo, hi, n = ev.mean_ci([1, None, 3, float("nan")])
    check("略過 None / NaN", n == 2 and abs(m - 2.0) < 1e-9, f"n={n} m={m}")

    # 樣本越多區間越窄——這是整份報告「不要只看點估計」的依據
    wide = ev.mean_ci(list(np.random.default_rng(1).normal(0, 1, 20)))
    narrow = ev.mean_ci(list(np.random.default_rng(1).normal(0, 1, 500)))
    check("樣本越多區間越窄",
          (wide[2] - wide[1]) > (narrow[2] - narrow[1]))


def test_thresholds_are_fixed():
    print("\n[3] 判斷標準寫死在常數裡（不是看資料才決定）")
    check("Gate 1 門檻 = 50%", ev.GATE1_MIN_LOWER_BOUND == 50.0)
    check("Gate 3 安全邊際 = 1.5 倍", ev.GATE3_COST_SAFETY == 1.5)
    check("樣本門檻 = 30 筆", ev.MIN_TRADES_FOR_CONCLUSION == 30)
    src = (ROOT / "tools" / "evaluate.py").read_text(encoding="utf-8")
    check("常數定義在模組層，不在函式內",
          "\nGATE1_MIN_LOWER_BOUND = " in src)
    check("verdict() 對 None 回傳「無法判斷」而不是「不通過」",
          ev.verdict(None) == "無法判斷")
    check("verdict(True) = 通過", ev.verdict(True) == "通過")
    check("verdict(False) = 不通過", ev.verdict(False) == "不通過")


# ----------------------------------------------------------------------
def make_log(path, lower_bound, n_rows=130, target_dates=None, prices=None):
    cols = analyzer_columns()
    assert cols, "取不到 EXCEL_LOG_COLUMNS"
    rows = []
    for i in range(n_rows):
        r = {c: None for c in cols}
        d = dt.date(2026, 8, 31) + dt.timedelta(days=i % 13)
        r.update({
            "執行時間": dt.datetime.combine(d, dt.time(15, 10)),
            "股票代碼": f"{2330 + (i % 10)}.TW",
            "預測目標日(隔日估計)": (d + dt.timedelta(days=1)).isoformat(),
            "目前股價": 100.0 + (i % 10),
            "資料是否停滯": False,
            "隔日_邏輯迴歸_樣本外準確率(%)": lower_bound + 5,
            "隔日_邏輯迴歸_樣本數": 200,
            "隔日_邏輯迴歸_準確率信賴下限(%)": lower_bound,
            "隔日_RF_樣本外準確率(%)": lower_bound + 4,
            "隔日_RF_樣本數": 200,
            "隔日_RF_準確率信賴下限(%)": lower_bound - 1,
            "程式版本": 3.7,
        })
        rows.append(r)
    pd.DataFrame(rows)[cols].to_excel(path, index=False, sheet_name="分析紀錄")


def run_gate1(lower_bound):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "log.xlsx"
        make_log(p, lower_bound)
        df = ev.read_log(p)
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = ev.gate1_edge(df)
        return res, buf.getvalue()


def test_gate1():
    print("\n[4] Gate 1：模型有沒有 edge")
    res, out = run_gate1(46.2)
    check("下界 46.2% → 不通過", res is False, str(res))
    check("不通過時明講不要調模型參數", "不要去調模型參數" in out)
    check("不通過時明講正報酬不能歸因於模型", "不能歸因於模型" in out)

    res, out = run_gate1(57.2)
    check("下界 57.2% → 通過", res is True, str(res))

    res, out = run_gate1(50.0)
    check("下界剛好 50.0% → 不通過（要嚴格大於）", res is False, str(res))

    # 沒有下界欄位時必須回「無法判斷」，不能當成「不通過」
    df = pd.DataFrame({"股票代碼": ["2330.TW"], "綜合分數": [3]})
    buf = io.StringIO()
    with redirect_stdout(buf):
        res = ev.gate1_edge(df)
    check("缺欄位 → 無法判斷（不是不通過）", res is None, str(res))
    check("缺欄位時說明原因", "沒有" in buf.getvalue())


# ----------------------------------------------------------------------
def build_sim(td: Path, n_days=13, seed=5):
    """用真的模擬器跑一輪，產出真的 state.json。"""
    pt = load_pt()
    cols = analyzer_columns()
    rng = np.random.default_rng(seed)
    syms = [f"{2330 + i}.TW" for i in range(10)]
    days = [d for d in (dt.date(2026, 8, 31) + dt.timedelta(days=i)
                        for i in range(26)) if d.weekday() < 5][:n_days]
    px = {s: float(rng.uniform(50, 500)) for s in syms}

    rows, prow = [], []
    for s in syms:
        prow.append({"date": (days[0] - dt.timedelta(days=1)).isoformat(),
                     "symbol": s, "open": px[s], "close": px[s]})
    for d in days:
        for s in syms:
            px[s] *= (1 + rng.normal(0.0005, 0.015))
            o = px[s] * (1 + rng.normal(0.0, 0.004))
            c = o * (1 + rng.normal(0.0, 0.012))
            prow.append({"date": d.isoformat(), "symbol": s,
                         "open": round(o, 2), "close": round(c, 2)})
            r = {k: None for k in cols}
            r.update({
                "執行時間": dt.datetime.combine(d, dt.time(15, 10)),
                "股票代碼": s, "公司名稱": s,
                "股價日期(資料基準日)": d.isoformat(),
                "預測目標日(隔日估計)": (d + dt.timedelta(days=1)).isoformat(),
                "目前股價": round(c, 2), "資料是否停滯": False,
                "綜合分數": int(rng.integers(-3, 6)),
                "ATR": round(c * 0.025, 2),
                "隔日_邏輯迴歸_上漲機率(%)": round(rng.uniform(42, 60), 1),
                "隔日_邏輯迴歸_樣本外準確率(%)": 51.0,
                "隔日_邏輯迴歸_樣本數": 200,
                "隔日_邏輯迴歸_準確率信賴下限(%)": 46.0,
                "隔日_RF_上漲機率(%)": round(rng.uniform(42, 60), 1),
                "隔日_RF_樣本外準確率(%)": 50.0,
                "隔日_RF_樣本數": 200,
                "隔日_RF_準確率信賴下限(%)": 45.0,
                "Strategy_Decision": "觀望", "程式版本": 3.7,
            })
            rows.append(r)

    log = td / "stock_analysis_log_v3.7.xlsx"
    pd.DataFrame(rows)[cols].to_excel(log, index=False, sheet_name="分析紀錄")
    pf = td / "px.csv"
    pd.DataFrame(prow).to_csv(pf, index=False)
    state = td / "paper_trading_state.json"
    pt.main(["init", "--state", str(state), "--capital", "1000000", "--top-n", "5"])
    for d in days:
        pt.main(["step", "--state", str(state), "--date", d.isoformat(),
                 "--log", str(log), "--offline-prices", str(pf)])
    return pt, pt.Simulator.load(state), log, state


def test_end_to_end():
    print("\n[5] 端到端：用真的模擬器產生的資料跑完四道關卡")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        buf = io.StringIO()
        with redirect_stdout(buf):
            pt, sim, log, state = build_sim(td)
        check("模擬有實際成交", not sim.all_trades().empty)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ev.main(["--log", str(log), "--state", str(state)])
        out = buf.getvalue()
        check("執行成功", rc == 0)
        for gate in ("Gate 1", "Gate 2", "Gate 3", "Gate 4"):
            check(f"報告含 {gate}", gate in out)
        check("報告含結論", "結論" in out)
        check("列出判斷標準（在數字之前）",
              out.index("判斷標準在看到資料之前") < out.index("Gate 1"))
        check("Gate 2 有信賴區間", "95% 信賴區間" in out or "信賴區間" in out)
        check("Gate 3 有年化成本", "年化成本" in out)
        check("沒有把小樣本講成結論",
              "無法判斷" in out or "樣本數不足" in out or "需要 30 筆" in out)


def test_missing_inputs():
    print("\n[6] 缺檔案時不能給出看起來像結論的東西")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        log = td / "stock_analysis_log_v3.7.xlsx"
        make_log(log, 46.0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ev.main(["--log", str(log), "--state", str(td / "無.json")])
        out = buf.getvalue()
        check("沒有 state 仍能執行", rc == 0)
        check("明講跳過 Gate 2/3", "跳過" in out)
        check("Gate 2 標為無法判斷", "Gate 2  有沒有贏過不做         無法判斷" in out,
              [l for l in out.splitlines() if "Gate 2" in l])
        check("仍給出 Gate 1 結論", "Gate 1  模型有沒有 edge        不通過" in out,
              [l for l in out.splitlines() if "Gate 1" in l])

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ev.main(["--log", str(td / "無.xlsx"), "--state", str(td / "無.json")])
        out = buf.getvalue()
        check("兩個檔案都沒有也不會炸", rc == 0)
        check("全部標為無法判斷", out.count("無法判斷") >= 3, out[-400:])


def test_slippage_pairing():
    print("\n[7] 滑價必須用（代碼, 日期）配對")
    cols = analyzer_columns()
    rows = []
    for i, d in enumerate([dt.date(2026, 9, 1), dt.date(2026, 9, 2)]):
        r = {c: None for c in cols}
        r.update({"股票代碼": "2330.TW",
                  "預測目標日(隔日估計)": d.isoformat(),
                  "目前股價": 100.0 if i == 0 else 200.0})
        rows.append(r)
    df = pd.DataFrame(rows)[cols]
    trades = pd.DataFrame([
        {"策略": "a", "date": "2026-09-01", "symbol": "2330.TW",
         "side": "買進", "price": 101.0},
        {"策略": "a", "date": "2026-09-02", "symbol": "2330.TW",
         "side": "買進", "price": 202.0},
    ])
    slip = ev._gap_slippage(trades, df)
    check("兩筆都配對成功", slip is not None and len(slip) == 2, str(slip))
    check("9/1 滑價 +1%（101/100）", abs(slip[0] - 1.0) < 1e-6, str(slip))
    check("9/2 滑價 +1%（202/200，沒有拿 100 當基準）",
          abs(slip[1] - 1.0) < 1e-6, str(slip))

    # 日期對不上就不該硬湊一個數字出來
    trades2 = trades.copy()
    trades2["date"] = "2026-12-25"
    check("日期完全對不上時回傳 None，而不是硬算",
          ev._gap_slippage(trades2, df) is None)

    # 代碼後綴不同也要能配對
    trades3 = trades.copy()
    trades3["symbol"] = "2330"
    s3 = ev._gap_slippage(trades3, df)
    check("代碼有無 .TW 都能配對", s3 is not None and len(s3) == 2, str(s3))


def test_same_day_stops():
    print("\n[8] 進場當天就停損的偵測")
    trades = pd.DataFrame([
        {"策略": "a", "date": "2026-09-01", "symbol": "X", "side": "買進"},
        {"策略": "a", "date": "2026-09-01", "symbol": "X", "side": "賣出",
         "reason": "停損觸發（收盤 90）", "報酬率(%)": -7.0},
        {"策略": "a", "date": "2026-09-02", "symbol": "Y", "side": "買進"},
        {"策略": "a", "date": "2026-09-05", "symbol": "Y", "side": "賣出",
         "reason": "停損觸發（收盤 80）", "報酬率(%)": -8.0},
        {"策略": "a", "date": "2026-09-03", "symbol": "Z", "side": "買進"},
        {"策略": "a", "date": "2026-09-03", "symbol": "Z", "side": "賣出",
         "reason": "已跌出今日名單，換股", "報酬率(%)": 0.5},
    ])
    found = ev._same_day_stops(trades)
    check("抓到當天進當天停損的 X", len(found) == 1 and found[0]["symbol"] == "X",
          str([f["symbol"] for f in found]))
    check("隔幾天才停損的 Y 不算", all(f["symbol"] != "Y" for f in found))
    check("當天換股（非停損）的 Z 不算", all(f["symbol"] != "Z" for f in found))


def test_stock_py_wiring():
    print("\n[9] stock.py 接線")
    src = (ROOT / "stock.py").read_text(encoding="utf-8")
    check("evaluate 有接到 dispatcher", 'cmd == "evaluate"' in src)
    check("evaluate 列在工具清單", '"evaluate.py"' in src)
    check("總覽畫面列出 evaluate", "evaluate 獲利能力評估" in src)

    spec = importlib.util.spec_from_file_location("stock_entry", ROOT / "stock.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    calls = []
    m.run = lambda tool, args=(): calls.append((tool, list(args))) or 0
    m.main(["evaluate", "--log", "X.xlsx"])
    check("evaluate 參數有原樣轉發",
          calls[-1][0] == "evaluate.py" and "--log" in calls[-1][1], str(calls[-1:]))


def main():
    print("=" * 64)
    print("  獲利能力評估 離線測試")
    print("=" * 64)
    test_wilson()
    test_mean_ci()
    test_thresholds_are_fixed()
    test_gate1()
    test_end_to_end()
    test_missing_inputs()
    test_slippage_pairing()
    test_same_day_stops()
    test_stock_py_wiring()
    print("\n" + "=" * 64)
    print(f"  通過 {_passed} 項 / 失敗 {_failed} 項")
    print("=" * 64)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
