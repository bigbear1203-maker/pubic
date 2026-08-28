# -*- coding: utf-8 -*-
"""
活躍股預測程式 v2.1 vs v3.0 實證比較工具
==========================================
回答一個問題：**這兩支程式選出來的股票，哪一個後來真的比較會漲？**

為什麼需要這支程式
------------------
v2.1 和 v3.0 都是「活躍度排序工具」，兩支程式的 docstring 也都誠實說明了
自己不是報酬預測模型。所以「哪個選股比較好」不能靠讀程式碼判斷——
必須拿真實資料測。這支程式就是做這件事。

它做的事：
  1. 抓一次 TWSE 全市場面板（兩支程式共用，不重複抓）
  2. 在多個歷史時間點 T，分別用 v2.1 與 v3.0 的演算法選出前 N 名
  3. 測量這些標的在 T+1、T+3、T+5 的實際報酬
  4. 與三個基準比較：全市場平均、隨機選 N 檔、成交值最大的 N 檔
  5. 同時測「活躍度」本身有沒有延續（選出來的隔天是不是真的還很活躍）

這是 walk-forward 測試：每個 T 只用 T 當天（含）以前的資料選股，
再看之後發生什麼，沒有前視偏誤。

⚠ 必讀
------
  • 這支程式測的是「活躍度排序」與「後續報酬」的關係。就算某一版
    在這次測試中報酬較高，也不代表它是好的選股工具——見結果最後的
    統計檢定力說明。
  • 活躍度高同時代表波動大。報酬平均值高但標準差更高，對投資人
    不一定是好事，所以下方也會印出標準差與最差單筆。
  • 需要能連 twse.com.tw。抓 60 個交易日大約 2~3 分鐘。

使用方式
--------
    python compare_predictors.py                      # 預設抓 60 個交易日
    python compare_predictors.py --days 90 --top-n 10
    python compare_predictors.py -o 比較報告.xlsx
    python compare_predictors.py --panel-cache panel.csv   # 存/讀面板，免得重抓

不構成投資建議。
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent


def _find(name: str) -> Path | None:
    for c in (SCRIPT_DIR / name, SCRIPT_DIR.parent / name,
              SCRIPT_DIR / "tools" / name, SCRIPT_DIR.parent / "tools" / name):
        if c.exists():
            return c
    return None


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ============================================================
# 面板資料
# ============================================================

def fetch_panel(v30_module, n_days: int) -> pd.DataFrame:
    """
    用 v3.0 的 fetch_twse_day 抓面板，但**不套用 is_common_stock 過濾**——
    過濾是兩支程式的差異點之一，必須留到比較階段才套用，否則就測不出
    「過濾掉 ETF 有沒有用」這件事。
    """
    frames = []
    current = dt.date.today() - dt.timedelta(days=1)
    attempts, max_attempts = 0, n_days * 3 + 20

    while len(frames) < n_days and attempts < max_attempts:
        attempts += 1
        if current.weekday() < 5:
            ds = current.strftime("%Y%m%d")
            print(f"  抓取 {ds} ...", end="")
            df = _fetch_day_unfiltered(v30_module, ds)
            if df is not None and not df.empty:
                print(f" OK（{len(df)} 檔）")
                frames.append(df)
            else:
                print(" 無資料")
            time.sleep(1.0)
        current -= dt.timedelta(days=1)

    if not frames:
        raise RuntimeError("抓不到任何交易日資料，請檢查網路或 TWSE 服務狀態")
    panel = pd.concat(list(reversed(frames)), ignore_index=True)
    return panel.drop_duplicates(subset=["date", "stock_id"]).reset_index(drop=True)


def _fetch_day_unfiltered(v30, date_str: str):
    """複用 v3.0 的抓取與清理，但把普通股過濾拿掉。"""
    import requests
    params = {"response": "json", "date": date_str, "type": "ALLBUT0999"}
    try:
        resp = requests.get(v30.TWSE_URL, params=params, headers=v30.HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f" 失敗 {e}", end="")
        return None
    if data.get("stat") != "OK":
        return None
    target = None
    for t in data.get("tables", []):
        if isinstance(t, dict) and "證券代號" in t.get("fields", []):
            target = t
            break
    if target is None or not target.get("data"):
        return None

    df = pd.DataFrame(target["data"], columns=target["fields"])
    if [c for c in v30.NEEDED_COLUMNS if c not in df.columns]:
        return None
    df = df[list(v30.NEEDED_COLUMNS.keys())].rename(columns=v30.NEEDED_COLUMNS)
    for col in ["Trading_Volume", "Trading_turnover", "Trading_money",
                "open", "max", "min", "close"]:
        df[col] = (df[col].astype(str).str.replace(",", "", regex=False).str.strip()
                   .replace({"--": np.nan, "---": np.nan, "": np.nan}))
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    df = df.dropna(subset=["close"])
    return df[df["close"] > 0].reset_index(drop=True)


# ============================================================
# 選股
# ============================================================

def picks_v21(v21, panel: pd.DataFrame, dates: list[str], top_n: int) -> list[str]:
    """v2.1：近 5 個交易日，不過濾 ETF/權證。"""
    window = dates[-v21.LOOKBACK_DAYS:]
    p = panel[panel["date"].isin(window)].copy()
    if p.empty:
        return []
    scored = v21.compute_daily_scores(p)
    pred = v21.predict_next_day_activity(scored)
    if pred.empty:
        return []
    pred = pred.sort_values("predicted_next_score", ascending=False)
    return pred.head(top_n)["stock_id"].tolist()


def picks_v30(v30, panel: pd.DataFrame, dates: list[str], top_n: int) -> list[str]:
    """v3.0：近 30 個交易日，過濾非普通股，最低歷史天數門檻。"""
    window = dates[-v30.LOOKBACK_DAYS:]
    p = panel[panel["date"].isin(window)].copy()
    p = p[p.apply(lambda r: v30.is_common_stock(r["stock_id"], r["stock_name"]), axis=1)]
    if p.empty:
        return []
    scored = v30.compute_daily_scores(p)
    pred = v30.predict_next_day_activity(scored)
    if pred.empty:
        return []
    return pred.head(top_n)["stock_id"].tolist()


def picks_biggest_money(panel: pd.DataFrame, dates: list[str], top_n: int) -> list[str]:
    """基準：單純挑當日成交值最大的 N 檔（最笨的活躍度定義）。"""
    day = panel[panel["date"] == dates[-1]]
    return day.nlargest(top_n, "Trading_money")["stock_id"].tolist()


def picks_random(panel: pd.DataFrame, dates: list[str], top_n: int, rng) -> list[str]:
    day = panel[panel["date"] == dates[-1]]
    ids = day["stock_id"].unique()
    if len(ids) <= top_n:
        return list(ids)
    return list(rng.choice(ids, size=top_n, replace=False))


# ============================================================
# 評估
# ============================================================

def forward_return(panel_wide: pd.DataFrame, symbols: list[str],
                   t_idx: int, horizon: int) -> float | None:
    """T 日收盤 → T+horizon 日收盤的等權平均報酬（%）。"""
    if not symbols or t_idx + horizon >= len(panel_wide.index):
        return None
    d0, d1 = panel_wide.index[t_idx], panel_wide.index[t_idx + horizon]
    rets = []
    for s in symbols:
        if s not in panel_wide.columns:
            continue
        a, b = panel_wide.at[d0, s], panel_wide.at[d1, s]
        if pd.notna(a) and pd.notna(b) and a > 0:
            rets.append((b / a - 1) * 100)
    return float(np.mean(rets)) if rets else None


def activity_persistence(panel: pd.DataFrame, symbols: list[str],
                         dates: list[str], t_idx: int) -> float | None:
    """
    選出的標的在 T+1 的成交值，在全市場中的百分位。
    測的是「活躍度有沒有延續」——這才是活躍度排序工具該負責的事。
    """
    if not symbols or t_idx + 1 >= len(dates):
        return None
    nxt = panel[panel["date"] == dates[t_idx + 1]]
    if nxt.empty:
        return None
    ranks = nxt["Trading_money"].rank(pct=True)
    idx = nxt["stock_id"].isin(symbols)
    return float(ranks[idx].mean() * 100) if idx.any() else None


def _wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, (c - h) * 100), min(100.0, (c + h) * 100))


def run_comparison(panel: pd.DataFrame, v21, v30, top_n: int,
                   horizons=(1, 3, 5), seed: int = 42) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = sorted(panel["date"].unique())
    panel_wide = panel.pivot_table(index="date", columns="stock_id",
                                   values="close", aggfunc="last")
    panel_wide = panel_wide.reindex(dates)
    rng = np.random.default_rng(seed)

    # 至少要有 v3.0 需要的 30 天，再留最長 horizon 的未來
    start = max(v30.LOOKBACK_DAYS, v21.LOOKBACK_DAYS)
    end = len(dates) - max(horizons)
    if end <= start:
        raise RuntimeError(
            f"資料不足：需要至少 {start + max(horizons) + 1} 個交易日，"
            f"目前只有 {len(dates)} 個。請加大 --days。")

    market_ids = panel["stock_id"].unique().tolist()
    rows, detail = [], []

    for t in range(start, end):
        window = dates[:t + 1]
        sets = {
            "v2.1（近5日，未過濾）": picks_v21(v21, panel, window, top_n),
            "v3.0（近30日，僅普通股）": picks_v30(v30, panel, window, top_n),
            "基準：成交值最大N檔": picks_biggest_money(panel, window, top_n),
            "基準：隨機N檔": picks_random(panel, window, top_n, rng),
            "基準：全市場等權": market_ids,
        }
        for name, syms in sets.items():
            rec = {"T日": dates[t], "策略": name, "選中檔數": len(syms)}
            for h in horizons:
                rec[f"T+{h}報酬(%)"] = forward_return(panel_wide, syms, t, h)
            rec["隔日成交值百分位"] = activity_persistence(panel, syms, dates, t)
            if name.startswith(("v2.1", "v3.0")):
                rec["選股清單"] = ",".join(syms)
            rows.append(rec)

        # 兩版重疊度
        a, b = set(sets["v2.1（近5日，未過濾）"]), set(sets["v3.0（近30日，僅普通股）"])
        detail.append({"T日": dates[t], "v2.1∩v3.0": len(a & b),
                       "僅v2.1": len(a - b), "僅v3.0": len(b - a)})

    return pd.DataFrame(rows), pd.DataFrame(detail)


def summarize(results: pd.DataFrame, horizons=(1, 3, 5)) -> pd.DataFrame:
    """
    除了各策略的絕對報酬，另外算「相對全市場的超額報酬」與其標準誤。

    為什麼一定要算超額報酬：同一期內全市場一起漲跌，所以各策略的絕對
    報酬高度相關。單看「v2.1 +0.21% vs 全市場 +0.04%」會誤以為有優勢，
    但那個差距可能遠小於它自己的波動。逐期相減之後，大盤的共同成分被
    消掉，剩下的才是這個策略真正的貢獻——再除以標準誤，就知道它跟 0
    分不分得開。|t| < 2 大致就是「跟沒有優勢分不出來」。
    """
    market = results[results["策略"].str.contains("全市場")]
    mkt_by_date = {h: dict(zip(market["T日"], market[f"T+{h}報酬(%)"])) for h in horizons}

    out = []
    for name, g in results.groupby("策略", sort=False):
        rec = {"策略": name, "測試期數": len(g)}
        for h in horizons:
            col = f"T+{h}報酬(%)"
            ex = []
            for _, r in g.iterrows():
                m = mkt_by_date[h].get(r["T日"])
                if pd.notna(r[col]) and m is not None and pd.notna(m):
                    ex.append(r[col] - m)
            if ex:
                arr = np.array(ex, dtype=float)
                rec[f"T+{h}超額報酬(%)"] = arr.mean()
                # 重疊視窗修正：T 逐日推進、但報酬看的是 T→T+h，所以相鄰
                # 期別的報酬窗格重疊了 h-1 天，觀察值並不獨立。若直接用
                # n 個觀察值算標準誤，會把 t 值灌大約 sqrt(h) 倍——實測在
                # 「報酬與活躍度完全無關」的合成資料上，T+5 的 t 值會膨脹到
                # 3.3，看起來像顯著優勢，其實只是重疊造成的假象。
                # 這裡用最保守的做法：有效樣本數取 n/h。
                n_eff = max(len(arr) / h, 1.0)
                se = arr.std(ddof=1) / math.sqrt(n_eff) if len(arr) > 1 else np.nan
                rec[f"T+{h}超額標準誤"] = se
                rec[f"T+{h}有效樣本數"] = n_eff
                rec[f"T+{h}超額t值"] = arr.mean() / se if se and se > 0 else np.nan
        for h in horizons:
            col = f"T+{h}報酬(%)"
            v = g[col].dropna()
            rec[f"T+{h}平均報酬(%)"] = v.mean() if len(v) else np.nan
            rec[f"T+{h}報酬標準差"] = v.std(ddof=1) if len(v) > 1 else np.nan
            rec[f"T+{h}最差單期(%)"] = v.min() if len(v) else np.nan
            if len(v):
                k = int((v > 0).sum())
                rec[f"T+{h}上漲期數比例(%)"] = k / len(v) * 100
                lo, hi = _wilson(k, len(v))
                rec[f"T+{h}比例95%下限"] = lo
        ap = g["隔日成交值百分位"].dropna()
        rec["隔日成交值百分位"] = ap.mean() if len(ap) else np.nan
        out.append(rec)
    return pd.DataFrame(out)


def print_report(summary: pd.DataFrame, overlap: pd.DataFrame,
                 results: pd.DataFrame, horizons=(1, 3, 5)) -> None:
    line = "=" * 78
    n = int(summary["測試期數"].max()) if len(summary) else 0
    print(f"\n{line}\n  活躍股預測 v2.1 vs v3.0 實證比較\n{line}")
    print(f"  walk-forward 測試期數：{n}（每期只用當期以前的資料選股）")

    print(f"\n【後續報酬：這些選股後來漲了嗎？】")
    cols = ["策略", "測試期數"] + [f"T+{h}平均報酬(%)" for h in horizons]
    print(summary[cols].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))

    print(f"\n【風險面：平均高不代表好】")
    cols = ["策略"] + [f"T+{h}報酬標準差" for h in horizons] + \
           [f"T+{h}最差單期(%)" for h in horizons]
    print(summary[cols].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))

    print(f"\n【相對全市場的超額報酬（已消掉大盤共同漲跌）】")
    cols = ["策略"] + sum([[f"T+{h}超額報酬(%)", f"T+{h}超額t值"] for h in horizons], [])
    cols = [c for c in cols if c in summary.columns]
    print(summary[cols].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    print("  ※ |t| < 2 代表這個超額報酬跟 0 分不出來，也就是「沒有可測量的優勢」。")
    print("  ※ t 值已針對重疊視窗修正（T+h 的相鄰期別重疊 h-1 天，有效樣本數取 n/h）。")
    print("     未修正的話，光是重疊就能讓 T+5 的 t 值虛增約 2.2 倍。")

    h = horizons[0]
    print(f"\n【T+{h} 上漲期數比例（含 95% 信賴區間下限）】")
    cols = ["策略", f"T+{h}上漲期數比例(%)", f"T+{h}比例95%下限"]
    print(summary[cols].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))
    print("  ※ 只有「95%下限 > 50」才算真的比擲硬幣好。")

    print(f"\n【活躍度延續：選出的標的隔天還活躍嗎？】")
    print("  （成交值在全市場的百分位，越接近 100 代表越活躍）")
    print(summary[["策略", "隔日成交值百分位"]].to_string(
        index=False, float_format=lambda v: f"{v:,.1f}"))
    print("  ※ 這才是活躍度排序工具真正該負責的指標。")

    if not overlap.empty:
        print(f"\n【兩版選股重疊度】")
        print(f"  平均交集 {overlap['v2.1∩v3.0'].mean():.1f} 檔"
              f"／僅 v2.1 {overlap['僅v2.1'].mean():.1f} 檔"
              f"／僅 v3.0 {overlap['僅v3.0'].mean():.1f} 檔")

    # 直接下結論的那一段
    print(f"\n{line}")
    print("【怎麼判讀】")
    try:
        s21 = summary[summary["策略"].str.startswith("v2.1")].iloc[0]
        s30 = summary[summary["策略"].str.startswith("v3.0")].iloc[0]
        mkt = summary[summary["策略"].str.contains("全市場")].iloc[0]
        for hh in horizons:
            c, tc = f"T+{hh}平均報酬(%)", f"T+{hh}超額t值"
            t21, t30 = s21.get(tc, np.nan), s30.get(tc, np.nan)

            def verdict(tv):
                if pd.isna(tv):
                    return "無法判斷"
                if abs(tv) < 2:
                    return "與全市場無法區分"
                return "顯著優於全市場" if tv > 0 else "顯著劣於全市場"

            print(f"  T+{hh}: v2.1 {s21[c]:+.3f}%（超額 t={t21:+.2f} → {verdict(t21)}）")
            print(f"        v3.0 {s30[c]:+.3f}%（超額 t={t30:+.2f} → {verdict(t30)}）")
            print(f"        全市場 {mkt[c]:+.3f}%")

        print(f"\n  活躍度延續（這才是這兩支程式該被評價的指標）：")
        print(f"    v2.1 {s21['隔日成交值百分位']:.1f} 分位／"
              f"v3.0 {s30['隔日成交值百分位']:.1f} 分位"
              f" → {'v3.0' if s30['隔日成交值百分位'] > s21['隔日成交值百分位'] else 'v2.1'} 較佳")
    except Exception:
        pass

    print(f"\n  ⚠ 統計檢定力：目前 {n} 期。同一期內所有股票會一起漲跌，")
    print(f"     有效獨立樣本數接近期數而非選股檔數。要判定「平均報酬高」")
    print(f"     不是運氣，大致需要 100 期以上（約半年的交易日）。")
    print(f"  ⚠ 兩支程式的 docstring 都寫明自己不是報酬預測模型。若上面的")
    print(f"     報酬欄位兩版都貼近全市場平均，那就是它們誠實的樣子——")
    print(f"     該用「活躍度延續」那一欄來評價它們，不是用報酬。")
    print(f"{line}")
    print("本報告為歷史統計，不構成投資建議。\n")


# ============================================================
# CLI
# ============================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="活躍股預測 v2.1 vs v3.0 實證比較")
    ap.add_argument("--days", type=int, default=60, help="抓取幾個交易日（預設 60）")
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--horizons", type=int, nargs="+", default=[1, 3, 5])
    ap.add_argument("-o", "--output", type=Path, default=None, help="輸出 Excel 報告")
    ap.add_argument("--panel-cache", type=Path, default=None,
                    help="面板資料快取 CSV：存在就直接讀，不存在就抓完後存起來")
    args = ap.parse_args(argv)

    p21, p30 = _find("tw_active_stocks_predictor_v2.1.py"), _find("tw_active_stocks_predictor_v3.0.py")
    if p21 is None or p30 is None:
        print("✗ 找不到預測程式，請確認下列檔案與本程式在同一個資料夾或其上層：")
        print("    tw_active_stocks_predictor_v2.1.py")
        print("    tw_active_stocks_predictor_v3.0.py")
        return 1
    print(f"[載入] {p21.name} / {p30.name}")
    v21, v30 = load_module(p21, "v21"), load_module(p30, "v30")

    if args.panel_cache and args.panel_cache.exists():
        print(f"[面板] 讀取快取：{args.panel_cache}")
        panel = pd.read_csv(args.panel_cache, dtype={"stock_id": str})
    else:
        print(f"[面板] 抓取近 {args.days} 個交易日 TWSE 全市場資料"
              f"（約 {args.days * 1.2 / 60:.0f} 分鐘）...")
        panel = fetch_panel(v30, args.days)
        if args.panel_cache:
            panel.to_csv(args.panel_cache, index=False)
            print(f"[面板] 已存快取：{args.panel_cache}")

    dates = sorted(panel["date"].unique())
    print(f"[面板] {len(panel):,} 筆／{panel['stock_id'].nunique():,} 檔／"
          f"{len(dates)} 個交易日（{dates[0]} ~ {dates[-1]}）")

    print(f"\n[比較] walk-forward 逐期選股與評估...")
    results, overlap = run_comparison(panel, v21, v30, args.top_n, tuple(args.horizons))
    summary = summarize(results, tuple(args.horizons))
    print_report(summary, overlap, results, tuple(args.horizons))

    if args.output:
        with pd.ExcelWriter(args.output, engine="openpyxl") as w:
            summary.to_excel(w, sheet_name="總結", index=False)
            results.to_excel(w, sheet_name="逐期明細", index=False)
            overlap.to_excel(w, sheet_name="選股重疊度", index=False)
        print(f"報告已輸出：{args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
