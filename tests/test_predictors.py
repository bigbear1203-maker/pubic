# -*- coding: utf-8 -*-
"""
活躍股預測程式 v2.1 / v3.0 / v3.1 離線測試
============================================
不連網路，驗證 v3.1 的修正確實生效，以及 v2.1/v3.0 的已知問題確實存在。

執行：python tests/test_predictors.py
"""

import datetime as dt
import importlib.util
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
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
    p = ROOT / fname
    if not p.exists():
        return None
    spec = importlib.util.spec_from_file_location(modname, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_panel(n_stocks=200, n_days=35, seed=3, with_etf=True, with_nan=False):
    rng = np.random.default_rng(seed)
    ids = [f"{2000 + i:04d}" for i in range(n_stocks)]
    names = [f"公司{i}" for i in range(n_stocks)]
    if with_etf:
        ids += ["0050", "020020", "2891C", "00684R"]
        names += ["元大台灣50", "富邦ETN", "中信特別股", "元大反1"]
    scale = np.exp(rng.normal(17, 2.2, len(ids)))
    d, dates = dt.date(2026, 7, 1), []
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += dt.timedelta(days=1)
    rows = []
    for di, dd in enumerate(dates):
        for k, (sid, nm) in enumerate(zip(ids, names)):
            spike = rng.uniform(3, 20) if rng.random() < 0.03 else 1.0
            money = scale[k] * rng.lognormal(0, 0.5) * spike
            if with_nan and sid == "2005":
                money = np.nan
            vol = (money if not np.isnan(money) else 1e8) / rng.uniform(20, 400)
            c = float(rng.uniform(15, 900))
            amp = abs(rng.normal(0.025, 0.02)) + 0.002
            rows.append({"date": dd, "stock_id": sid, "stock_name": nm,
                         "Trading_money": money, "Trading_Volume": vol,
                         "Trading_turnover": vol / rng.uniform(800, 4000),
                         "open": c, "max": c * (1 + amp / 2),
                         "min": c * (1 - amp / 2), "close": c})
    return pd.DataFrame(rows)


def main():
    print("=" * 64)
    print("  活躍股預測程式 v2.1 / v3.0 / v3.1 離線測試")
    print("=" * 64)

    v21 = load("tw_active_stocks_predictor_v2.1.py", "v21")
    v30 = load("tw_active_stocks_predictor_v3.0.py", "v30")
    v31 = load("tw_active_stocks_predictor_v3.1.py", "v31")
    check("三個版本都載入成功", all(m is not None for m in (v21, v30, v31)))
    if v31 is None:
        return 1

    # ---------------------------------------------------------
    print("\n[1] v3.1 修正：抓取起點不再永遠跳過今天")
    check("盤中 10:00 → 起點為昨天（當日資料尚未發布）",
          v31.data_start_date(dt.datetime(2026, 8, 28, 10, 0)) == dt.date(2026, 8, 27))
    check("收盤後 14:30 → 起點為今天",
          v31.data_start_date(dt.datetime(2026, 8, 28, 14, 30)) == dt.date(2026, 8, 28))
    check("收盤後 15:00 → 起點為今天",
          v31.data_start_date(dt.datetime(2026, 8, 28, 15, 0)) == dt.date(2026, 8, 28))
    check("v3.0 沒有這個函式（確認這是 v3.1 才有的修正）",
          not hasattr(v30, "data_start_date"))

    # ---------------------------------------------------------
    print("\n[2] v3.1 修正：名稱關鍵字不再誤殺合法普通股")
    keep = [("6024", "群益期"), ("2820", "華票"), ("2887", "台新金"),
            ("6015", "宏遠證"), ("2812", "台中銀"), ("9945", "潤泰新")]
    for sid, nm in keep:
        check(f"{sid} {nm} 保留", v31.is_common_stock(sid, nm))
    check("v3.0 會誤殺群益期(6024)（確認問題確實存在）",
          not v30.is_common_stock("6024", "群益期"))

    print("\n[3] 非普通股仍然全部排除")
    # 0050 / 0056 是 4 碼純數字的 ETF，光靠「只留 4 碼」的規則擋不掉，
    # 而它們正是全市場成交最熱的標的之一——這是 v3.0 的漏洞。
    for sid, nm in [("0050", "元大台灣50"), ("0056", "元大高股息"),
                    ("0051", "元大中型100"), ("020020", "富邦ETN"),
                    ("2891C", "中信金特別股"), ("00684R", "元大反1"),
                    ("006204", "永豐臺灣加權"), ("00919", "群益台灣精選高息")]:
        check(f"{sid} {nm} 排除", not v31.is_common_stock(sid, nm))
    check("v3.0 會漏掉 0050（確認這個漏洞確實存在）",
          v30.is_common_stock("0050", "元大台灣50"))
    check("v3.0 會漏掉 0056", v30.is_common_stock("0056", "元大高股息"))

    # ---------------------------------------------------------
    print("\n[4] 你 log 中那 4 檔問題標的會被排除")
    for s in ["006204", "00684R", "020020", "2891C"]:
        check(f"{s} 被排除", not v31.is_common_stock(s, ""))
    for s in ["1303", "2330", "2408", "3037", "8039", "8488"]:
        check(f"{s} 保留", v31.is_common_stock(s, ""))

    # ---------------------------------------------------------
    print("\n[5] v2.1 的已知問題（確認它們確實存在，不是我猜的）")
    dates5 = [f"2026-08-{d:02d}" for d in (24, 25, 26, 27, 28)]
    rows = []
    for d in dates5:
        rows.append({"date": d, "stock_id": "2330", "stock_name": "台積電",
                     "Trading_money": 1e10, "Trading_Volume": 1e6,
                     "Trading_turnover": 1e3, "open": 100, "max": 102,
                     "min": 98, "close": 100})
    for d in dates5[-2:]:
        rows.append({"date": d, "stock_id": "7777", "stock_name": "新上市",
                     "Trading_money": 3e8, "Trading_Volume": 9e5,
                     "Trading_turnover": 9e2, "open": 50, "max": 58,
                     "min": 49, "close": 55})
    p = pd.DataFrame(rows)
    r = v21.predict_next_day_activity(v21.compute_daily_scores(p))
    check("v2.1 只有 2 天資料的股票也會進排名",
          "7777" in r["stock_id"].tolist(), str(r["stock_id"].tolist()))
    check("v3.1 的 MIN_HISTORY_DAYS 會擋掉這種標的", v31.MIN_HISTORY_DAYS >= 15,
          str(v31.MIN_HISTORY_DAYS))
    check("v2.1 回看天數過短（5 天做線性回歸）", v21.LOOKBACK_DAYS <= 5,
          str(v21.LOOKBACK_DAYS))
    check("v3.1 回看 30 天", v31.LOOKBACK_DAYS >= 30, str(v31.LOOKBACK_DAYS))

    # ---------------------------------------------------------
    print("\n[6] 資料缺漏（TWSE 原始值為 '--'）的處理")
    panel = make_panel(n_stocks=60, n_days=32, with_nan=True)
    panel = panel[panel.apply(
        lambda r: v31.is_common_stock(r["stock_id"], r["stock_name"]), axis=1)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r31 = v31.predict_next_day_activity(v31.compute_daily_scores(panel))
    check("v3.1 把無法評分的標的排除於排名之外，不留 NaN",
          not r31["predicted_next_score"].isna().any())
    check("2005（成交金額缺值）不在排名中",
          "2005" not in r31["stock_id"].tolist())

    # ---------------------------------------------------------
    print("\n[7] v3.0/v3.1 的趨勢項實際貢獻（確認「預測」名不副實）")
    panel = make_panel(n_stocks=200, n_days=35)
    panel = panel[panel.apply(
        lambda r: v31.is_common_stock(r["stock_id"], r["stock_name"]), axis=1)]
    r31 = v31.predict_next_day_activity(v31.compute_daily_scores(panel))
    r31 = r31.copy()
    r31["trend_term"] = r31["trend_slope"] * r31["trend_strength"]
    ratio = (r31["trend_term"].abs()
             / r31["recent_avg_score"].abs().clip(lower=1e-9)).median()
    check(f"趨勢項貢獻極小（中位數佔比 {ratio * 100:.2f}%）", ratio < 0.05,
          f"{ratio:.4f}")
    top_full = r31.sort_values("predicted_next_score", ascending=False).head(10)
    top_avg = r31.sort_values("recent_avg_score", ascending=False).head(10)
    overlap = len(set(top_full["stock_id"]) & set(top_avg["stock_id"]))
    check(f"只用 recent_avg 排序，前10名幾乎相同（{overlap}/10）", overlap >= 9)
    check("→ 所以它實際上是「近3日平均活躍度排名」，不是預測", True)

    print("\n" + "=" * 64)
    print(f"  通過 {_passed} 項 / 失敗 {_failed} 項")
    print("=" * 64)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
