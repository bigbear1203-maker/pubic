# -*- coding: utf-8 -*-
"""
claude_stock_analyzer v3.7 離線測試
====================================
不連網路，用合成資料驗證 v3.7 的修正是否真的生效：

  1. 盤中執行會剔除未完成K棒，且盤中/收盤後跑出的隔日機率一致
     （這是 v3.6 最嚴重的問題：2891C.TW 同日差 39.2pp）
  2. 今日模型的標籤洩漏已修掉（訓練集不含被預測的那一列）
  3. 假日曆會跳過國定假日
  4. 資料停滯 / 資料不足會被擋下並記為 Skipped_*，不會混進「中性」
  5. Wilson 信賴下限關卡在小樣本高準確率時會擋下來
  6. Excel 紀錄欄位含樣本數與回填欄位

執行：python tests/test_v37_offline.py
"""

import datetime
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ANALYZER = ROOT / "claude_stock_analyzer_v3.7.py"

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def make_ohlcv(n_days=620, seed=7, end_date=None):
    """
    合成一段有波動、有量的日K資料。用固定種子確保可重現。

    ⚠ end_date 預設是固定日期（2026-08-28），這是為了讓測試結果可重現。
    但代價是：只要測試中有任何程式路徑會呼叫 datetime.now()，隨著真實
    日期往後推移，這批資料就會逐漸「變舊」而觸發資料停滯檢查——測到的
    就變成日曆而不是程式。因此**凡是會走到 analyze() 或新鮮度檢查的
    測試，都必須凍結時鐘**（見 [7] 與 [9] 的 _FrozenDT 寫法），
    或改用 now= 參數明確傳入時間點。
    """
    rng = np.random.default_rng(seed)
    end_date = end_date or datetime.date(2026, 8, 28)
    idx = pd.bdate_range(end=pd.Timestamp(end_date), periods=n_days, tz="Asia/Taipei")
    ret = rng.normal(0.0004, 0.018, n_days)
    close = 100 * np.exp(np.cumsum(ret))
    open_ = close * (1 + rng.normal(0, 0.005, n_days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n_days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n_days)))
    vol = rng.integers(5_000, 60_000, n_days).astype(float) * 1000
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def load_analyzer():
    spec = importlib.util.spec_from_file_location("analyzer_v37", ANALYZER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def install_offline_stubs(mod, hist, price_date=None):
    """把所有會連外的東西換掉，讓 analyze() 可以離線跑完整條路徑。"""

    class _FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol
            self.info = {"longName": "測試公司", "currency": "TWD",
                         "sector": "Technology", "industry": "Semiconductors"}

        def history(self, *a, **k):
            return hist.copy()

    mod.yf = types.SimpleNamespace(Ticker=_FakeTicker)
    mod.get_intermarket_features = lambda index, period="3y": (None, [])
    mod.fundamental_summary = lambda t, current_price=None: (
        ["（測試樁）"], 1, t.info, {"sector": "Technology", "industry": "Semiconductors"})
    mod.chip_summary = lambda sym, t, info=None: (["（測試樁）"], 0, None, {})
    return mod


def main():
    print("=" * 62)
    print("  claude_stock_analyzer v3.7 離線測試")
    print("=" * 62)

    # ---------------------------------------------------------------
    print("\n[1] 假日曆：next_trading_day_estimate 跳過國定假日")
    mod = load_analyzer()
    d, note = mod.next_trading_day_estimate(datetime.date(2026, 8, 28))  # 五
    check("週五 → 下週一", d == datetime.date(2026, 8, 31), f"得到 {d}")
    d, note = mod.next_trading_day_estimate(datetime.date(2026, 2, 13))  # 春節前
    check("春節前 → 跳過整個連假", d == datetime.date(2026, 2, 23), f"得到 {d}")
    d, note = mod.next_trading_day_estimate(datetime.date(2026, 4, 2))
    check("清明連假前 → 4/7", d == datetime.date(2026, 4, 7), f"得到 {d}")
    d, note = mod.next_trading_day_estimate(datetime.date(2027, 3, 1))
    check("超出假日清單涵蓋範圍時會提醒", bool(note), "沒有提醒訊息")

    # ---------------------------------------------------------------
    print("\n[2] Wilson 信賴下限：門檻隨樣本數自動調整（取代固定 55%）")
    # 固定 55% 門檻會放行的情況，但區間仍蓋住 50% → 應該擋下
    r = mod.assess_accuracy_reliability(56.0, 60)
    check("n=60 / 56%（舊規則會放行）→ 擋下", not r["reliable"], r["message"])
    # 固定 55% 門檻會擋下的情況，但樣本夠多、統計上站得住腳 → 應該放行
    r = mod.assess_accuracy_reliability(53.5, 2000)
    check("n=2000 / 53.5%（舊規則會擋下）→ 放行", r["reliable"], r["message"])
    r = mod.assess_accuracy_reliability(74.0, 300)
    check("n=300 / 74% → 可靠", r["reliable"], r["message"])
    r = mod.assess_accuracy_reliability(51.4, 450)
    check("n=450 / 51.4%（實測中位數）→ 不可靠", not r["reliable"], r["message"])
    r = mod.assess_accuracy_reliability(56.5, 450)
    check("n=450 / 56.5% → 可靠", r["reliable"], r["message"])
    r = mod.assess_accuracy_reliability(74.0, None)
    check("樣本數未知 → unknown 而非誤判為好", r["label"] == "unknown", str(r))

    # ---------------------------------------------------------------
    print("\n[3] 交易成本與 EV 決策")
    check("台股來回成本 ≈ 0.471%", abs(mod.round_trip_cost_pct() - 0.471) < 1e-9,
          str(mod.round_trip_cost_pct()))
    rel_ok = mod.assess_accuracy_reliability(58.0, 500)
    dec, det = mod.expectancy_decision(0.62, 1.8, 1.6, rel_ok)
    check("EV 未超過成本 → Wait", dec == "Wait", det["reason"])
    dec, det = mod.expectancy_decision(0.75, 2.5, 1.5, rel_ok)
    check("EV 明顯為正 → Buy", dec == "Buy", det["reason"])
    rel_bad = mod.assess_accuracy_reliability(51.0, 500)
    dec, det = mod.expectancy_decision(0.90, 5.0, 1.0, rel_bad)
    check("模型不可靠時，再高的機率也是 Wait", dec == "Wait", det["reason"])

    # ---------------------------------------------------------------
    print("\n[4] 未完成K棒剔除")
    hist = make_ohlcv(end_date=datetime.date(2026, 8, 28))
    intraday_now = datetime.datetime(2026, 8, 28, 11, 30)
    post_now = datetime.datetime(2026, 8, 28, 15, 0)
    h1, trimmed1, live = mod._trim_incomplete_bar(hist, "2330.TW", now=intraday_now)
    check("盤中 → 剔除最後一根", trimmed1 and len(h1) == len(hist) - 1)
    check("盤中 → 回傳當下價格", live is not None and abs(live - hist["Close"].iloc[-1]) < 1e-9)
    h2, trimmed2, _ = mod._trim_incomplete_bar(hist, "2330.TW", now=post_now)
    check("收盤後 → 不剔除", (not trimmed2) and len(h2) == len(hist))
    h3, trimmed3, _ = mod._trim_incomplete_bar(hist, "AAPL", now=intraday_now)
    check("美股不套用台股時段判斷", not trimmed3)

    # ---------------------------------------------------------------
    print("\n[5] 資料新鮮度與資料量把關")
    f = mod._check_price_freshness(hist, now=datetime.datetime(2026, 8, 28, 15, 0))
    check("當日資料 → 不停滯", not f["stale"], f["message"])
    f = mod._check_price_freshness(hist, now=datetime.datetime(2026, 9, 4, 15, 0))
    check("落後 7 天 → 判定停滯（00684R.TW 的情況）", f["stale"], f["message"])
    g = mod._should_analyze(make_ohlcv(n_days=100))
    check("資料僅 100 日 → 不足（020020.TW 的情況）", not g["ok"], g["reason"])
    flat = make_ohlcv(n_days=400)
    flat.loc[flat.index[-60:], "Close"] = 27.25
    check("近 60 日收盤價不動 → 擋下", not mod._should_analyze(flat)["ok"])
    check("正常資料 → 通過", mod._should_analyze(hist)["ok"])

    # ---------------------------------------------------------------
    print("\n[6] 今日模型標籤洩漏修正")
    ip = mod.intraday_close_probability_walkforward(hist, model="logistic",
                                                    last_bar_complete=True)
    check("今日模型可正常回傳", ip is not None)
    if ip is not None:
        check("回傳含樣本數", ip.get("sample_size", 0) > 0, str(ip.get("sample_size")))
        check("收盤後執行標記為結果已知", ip["result_already_known"] is True)
    ip2 = mod.intraday_close_probability_walkforward(hist, model="logistic",
                                                     last_bar_complete=False)
    if ip is not None and ip2 is not None:
        check("盤中模式的回測樣本少一筆（排除未完成末列）",
              ip2["sample_size"] == ip["sample_size"] - 1,
              f"{ip2['sample_size']} vs {ip['sample_size']}")
        check("盤中模式標記為結果未知", ip2["result_already_known"] is False)

    # ---------------------------------------------------------------
    print("\n[7] 端到端：同一個資料基準日只會有一個答案")
    print("    v3.6 的問題是：盤中跑與收盤後跑都宣稱基準日是 D，卻給出")
    print("    不同的機率（2891C.TW 差 39.2pp 且方向相反）。v3.7 之後，")
    print("    盤中執行會誠實地把基準日記成 D-1，因此")
    print("    「D 日盤中跑」與「D-1 日收盤後跑」必須得到完全相同的結果。")

    def run_once(hist_df, fake_now, symbol="2330.TW"):
        with tempfile.TemporaryDirectory() as td:
            m = load_analyzer()
            install_offline_stubs(m, hist_df, None)
            m.EXCEL_LOG_PATH = os.path.join(td, "log.xlsx")
            real_dt = m.datetime.datetime

            class _FrozenDT(real_dt):
                @classmethod
                def now(cls, tz=None):
                    return fake_now
            m.datetime.datetime = _FrozenDT
            try:
                m.analyze(symbol)
                return pd.read_excel(m.EXCEL_LOG_PATH).iloc[-1]
            finally:
                m.datetime.datetime = real_dt

    # A：8/28 盤中執行，資料含 8/28 未完成K棒 → 應剔除，基準日退回 8/27
    a = run_once(hist, datetime.datetime(2026, 8, 28, 11, 30))
    # B：8/27 收盤後執行，資料只到 8/27 → 基準日 8/27
    b = run_once(hist.iloc[:-1].copy(), datetime.datetime(2026, 8, 27, 15, 0))

    pa, pb = a["隔日_邏輯迴歸_上漲機率(%)"], b["隔日_邏輯迴歸_上漲機率(%)"]
    check("兩次執行都有輸出隔日機率", pd.notna(pa) and pd.notna(pb), f"{pa} / {pb}")
    check("資料基準日皆為 2026-08-27",
          str(a["股價日期(資料基準日)"])[:10] == "2026-08-27"
          and str(b["股價日期(資料基準日)"])[:10] == "2026-08-27",
          f"{a['股價日期(資料基準日)']} / {b['股價日期(資料基準日)']}")
    if pd.notna(pa) and pd.notna(pb):
        check(f"隔日機率完全一致（盤中 {pa:.4f}% vs 收盤後 {pb:.4f}%）", abs(pa - pb) < 1e-9,
              f"差 {abs(pa - pb):.4f}pp")
    check("預測目標日一致",
          str(a["預測目標日(隔日估計)"])[:10] == str(b["預測目標日(隔日估計)"])[:10],
          f"{a['預測目標日(隔日估計)']} vs {b['預測目標日(隔日估計)']}")
    check("基準日收盤價一致", abs(float(a["目前股價"]) - float(b["目前股價"])) < 1e-9,
          f"{a['目前股價']} vs {b['目前股價']}")
    check("盤中執行有被標記", bool(a["是否盤中執行"]) and not bool(b["是否盤中執行"]),
          f"{a['是否盤中執行']} / {b['是否盤中執行']}")
    check("盤中有另外記錄當下價格", pd.notna(a["執行當下價格"]))
    check("盤中的當下價格 ≠ 基準日收盤價（兩者不會被混為一談）",
          abs(float(a["執行當下價格"]) - float(a["目前股價"])) > 1e-9)

    # ---------------------------------------------------------------
    print("\n[8] Excel 欄位")
    for col in ["隔日_邏輯迴歸_樣本數", "隔日_RF_樣本數", "隔日_邏輯迴歸_準確率信賴下限(%)",
                "EV_Decision", "EV_淨期望值(%)", "實際目標日收盤", "實際報酬(%)",
                "是否命中_邏輯迴歸", "回填時間", "是否盤中執行", "今日模型是否已知結果"]:
        check(f"欄位存在：{col}", col in b.index)
    check("有記錄隔日模型樣本數", pd.notna(b["隔日_邏輯迴歸_樣本數"]),
          str(b.get("隔日_邏輯迴歸_樣本數")))

    # ---------------------------------------------------------------
    print("\n[9] 資料不足 / 停滯 → Skipped_*，不會混入「中性」")
    with tempfile.TemporaryDirectory() as td:
        m = load_analyzer()
        install_offline_stubs(m, make_ohlcv(n_days=120))
        m.EXCEL_LOG_PATH = os.path.join(td, "log.xlsx")
        # 必須凍結時鐘。合成資料的最後交易日固定在 2026-08-28，若用真實
        # 時間執行，隨著日子過去資料會「變舊」，先被資料停滯檢查攔下，
        # 這個測試就會改成收到 Skipped_資料停滯 而失敗——測到的是日曆，
        # 不是程式。（本測試確實在 2026-09-03 因此失敗過一次。）
        real_dt = m.datetime.datetime

        class _FrozenDT9(real_dt):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime(2026, 8, 28, 15, 0)
        m.datetime.datetime = _FrozenDT9
        try:
            m.analyze("020020.TW")
            df = pd.read_excel(m.EXCEL_LOG_PATH)
        finally:
            m.datetime.datetime = real_dt
        last = df.iloc[-1]
        check("資料不足 → Strategy_Decision = Skipped_資料不足",
              last["Strategy_Decision"] == "Skipped_資料不足", str(last["Strategy_Decision"]))
        check("資料不足 → 沒有綜合結論（不會寫成中性）",
              pd.isna(last["綜合結論"]) or last["綜合結論"] == "",
              repr(last["綜合結論"]))
        check("有記錄跳過原因", isinstance(last["跳過原因"], str) and last["跳過原因"],
              repr(last.get("跳過原因")))

    with tempfile.TemporaryDirectory() as td:
        m = load_analyzer()
        stale_hist = make_ohlcv(end_date=datetime.date(2026, 8, 20))
        install_offline_stubs(m, stale_hist, datetime.date(2026, 8, 20))
        m.EXCEL_LOG_PATH = os.path.join(td, "log.xlsx")
        real_dt = m.datetime.datetime

        class _FrozenDT2(real_dt):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime(2026, 8, 28, 15, 0)
        m.datetime.datetime = _FrozenDT2
        try:
            m.analyze("00684R.TW")
            df = pd.read_excel(m.EXCEL_LOG_PATH)
        finally:
            m.datetime.datetime = real_dt
        check("資料停滯 → Strategy_Decision = Skipped_資料停滯",
              df.iloc[-1]["Strategy_Decision"] == "Skipped_資料停滯",
              str(df.iloc[-1]["Strategy_Decision"]))

    # ---------------------------------------------------------------
    print("\n" + "=" * 62)
    print(f"  通過 {_passed} 項 / 失敗 {_failed} 項")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
