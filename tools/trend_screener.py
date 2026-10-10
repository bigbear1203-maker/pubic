# -*- coding: utf-8 -*-
"""
持續上漲股篩選 v1.0
====================
找的是「連續穩穩往上走」的股票，不是「成交量最大」的股票。

為什麼要另外寫一支，而不是調整活躍股篩選：
    活躍度高 = 波動大、話題多。實測 15 個交易日，以活躍股為選股池的
    所有策略都虧錢（-2% ~ -13%），同期加權指數約 +1.8%。使用者也觀察到：
    真正一路上漲的股票，很多根本不在成交量排行榜上。

學術依據（兩條獨立的證據指向同一件事）：

    1. Revisiting the momentum effect in Taiwan: The role of persistency
       (Pacific-Basin Finance Journal, 2023)
       台股長期被當成「動量效應的例外」，原因是贏家組合周轉太快——形成期
       一結束，約 46% 的贏家就掉出名單。把「持續待在贏家名單」的股票
       分出來之後，它們在中期（數個月）有顯著正報酬；不持續的那批則出現
       短期反轉，持有 6 個月平均每月 -0.786%。

    2. Frog in the Pan: Continuous Information and Momentum
       (Da, Gurun & Warachka, Review of Financial Studies, 2014)
       同樣的累積漲幅，靠「很多天小漲」累積出來的，後續 6 個月動量 8.86%；
       靠「少數幾天大漲」累積出來的只有 2.91%。衡量方式：
           ID = sign(形成期報酬) × (下跌日比例 − 上漲日比例)
       ID 越負，代表上漲越連續。後續研究發現這個效應主要出現在多頭市場。

這兩條加起來，就是使用者說的「連續上漲」：
    · 過去半年漲（形成期報酬 > 0，且在前 30%）
    · 再往前半年也漲（持續性，不是一次性的）
    · 漲的方式是天天小漲，不是幾根長紅（ID < 0，越負越好）

⚠ 這支程式不預測漲跌。它只是用有文獻支持的規則挑出候選名單，
  名單好不好，要靠紙上交易的 trend_persistent 策略去驗證。

⚠ 持有期是「月」不是「日」。文獻的結果是中期持有（數個月）得到的。
  如果每天換股，0.471% 的來回成本會把報酬吃光——模擬裡的
  trend_persistent 因此設計成每月才調整一次。

用法：
    python stock.py trend                 篩選並列出前 N 名
    python stock.py trend --top 10        列出前 10 名
    python stock.py trend --explain 2330  查某一檔為什麼入選或沒入選
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------

def _load_settings() -> dict:
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, here / "tools"):
        p = base / "stock_settings.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:                                 # noqa: BLE001
                print(f"  ⚠ stock_settings.json 讀取失敗，改用內建預設值：{e}")
                return {}
    return {}


_SETTINGS = _load_settings()


def _setting(key: str, default):
    try:
        v = _SETTINGS["趨勢股篩選"][key]
        return type(default)(v) if v is not None else default
    except (KeyError, TypeError, ValueError):
        return default


# 時間窗口（交易日）。這幾個是照文獻的慣例設定，不是用你的資料調出來的——
# 拿兩個月的資料去「找最佳窗口」就是過擬合。
SKIP_DAYS = _setting("skip_days", 21)          # 跳過最近一個月（台股短期反轉）
FORMATION_DAYS = _setting("formation_days", 126)  # 形成期 6 個月
TOP_FRACTION = _setting("top_fraction", 0.30)  # 贏家 = 形成期報酬前 30%
MIN_AVG_TURNOVER = _setting("min_avg_turnover", 20_000_000.0)  # 20 日均成交額下限（元）
MAX_RECENT_SPIKE = _setting("max_recent_spike", 0.25)  # 最近一個月漲超過 25% 不追
TOP_N = _setting("top_n", 5)
PREFILTER_N = _setting("prefilter_n", 400)     # 先依成交額取前幾檔再下載歷史

# 形成期 + 前一期 + 跳過的那個月 + 一點緩衝
HISTORY_DAYS_NEEDED = SKIP_DAYS + 2 * FORMATION_DAYS + 1


# ----------------------------------------------------------------------
# 核心計算（純函式，不碰網路）
# ----------------------------------------------------------------------

def _atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> float:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()],
                   axis=1).max(axis=1)
    v = tr.tail(n).mean()
    return float(v) if pd.notna(v) else float("nan")


def compute_metrics(close: pd.Series, volume: pd.Series | None = None,
                    high: pd.Series | None = None,
                    low: pd.Series | None = None) -> dict | None:
    """
    單一檔股票的趨勢指標。資料不足時回傳 None。

    所有窗口都以「最後一筆」為基準往回數，不碰任何未來資料。
    """
    close = pd.to_numeric(close, errors="coerce").dropna()
    if len(close) < HISTORY_DAYS_NEEDED:
        return None

    t = len(close) - 1
    i_form_end = t - SKIP_DAYS
    i_form_start = i_form_end - FORMATION_DAYS
    i_prior_start = i_form_start - FORMATION_DAYS
    if i_prior_start < 0:
        return None

    px = close.to_numpy(dtype=float)
    if min(px[i_prior_start], px[i_form_start], px[i_form_end], px[t]) <= 0:
        return None

    ret_form = px[i_form_end] / px[i_form_start] - 1
    ret_prior = px[i_form_start] / px[i_prior_start] - 1
    ret_recent = px[t] / px[i_form_end] - 1

    # 形成期內的每日報酬：算「上漲日比例」與 ID
    daily = np.diff(px[i_form_start:i_form_end + 1]) / px[i_form_start:i_form_end]
    daily = daily[np.isfinite(daily)]
    if len(daily) == 0:
        return None
    pct_pos = float((daily > 0).mean())
    pct_neg = float((daily < 0).mean())
    id_score = float(np.sign(ret_form) * (pct_neg - pct_pos))

    out = {
        "收盤價": float(px[t]),
        "形成期報酬(%)": ret_form * 100,
        "前一期報酬(%)": ret_prior * 100,
        "最近一個月(%)": ret_recent * 100,
        "上漲日比例(%)": pct_pos * 100,
        "連續性ID": id_score,
    }

    ma60 = float(close.tail(60).mean())
    out["站上60日均線"] = bool(px[t] > ma60)

    if volume is not None:
        vol = pd.to_numeric(volume, errors="coerce").reindex(close.index)
        turnover = (close * vol).tail(20).mean()
        out["20日均成交額"] = float(turnover) if pd.notna(turnover) else float("nan")
    else:
        out["20日均成交額"] = float("nan")

    if high is not None and low is not None:
        out["ATR"] = _atr(pd.to_numeric(high, errors="coerce").reindex(close.index),
                          pd.to_numeric(low, errors="coerce").reindex(close.index),
                          close)
    else:
        out["ATR"] = float("nan")
    return out


REJECT_HISTORY = "歷史資料不足（需要約 13 個月）"
REJECT_LIQUIDITY = "成交額太低，模擬成交不實際"
REJECT_NOT_WINNER = "近半年漲幅不在前段"
REJECT_NOT_PERSISTENT = "再往前半年沒有漲（不是持續上漲）"
REJECT_DISCRETE = "漲幅來自少數幾天大漲，不是連續上漲"
REJECT_SPIKE = "最近一個月漲太兇，短期反轉風險高"


def rank_universe(metrics: pd.DataFrame,
                  top_n: int = TOP_N,
                  top_fraction: float = TOP_FRACTION,
                  min_turnover: float = MIN_AVG_TURNOVER,
                  max_recent_spike: float = MAX_RECENT_SPIKE) -> pd.DataFrame:
    """
    在已算好指標的股票池裡套用篩選規則並排名。

    回傳每一檔都在的表，多兩欄：「入選」與「未入選原因」。
    保留落選的股票是故意的——使用者想知道「我看到一路漲的那檔，
    為什麼沒被選上」，答案就在那一欄。

    規則依序套用，第一個沒過的就是原因：
        1. 歷史資料足夠
        2. 流動性：20 日均成交額 ≥ min_turnover
        3. 贏家：形成期報酬 > 0 且在流動股中排前 top_fraction
        4. 持續：前一期報酬 > 0
        5. 連續：ID < 0（上漲日多於下跌日）
        6. 不追高：最近一個月漲幅 ≤ max_recent_spike
    通過者依 ID 由低到高排序（越連續越前面），同分比形成期報酬。
    """
    df = metrics.copy()
    df["入選"] = False
    df["未入選原因"] = ""
    if df.empty:
        df["排名"] = pd.Series(dtype="Int64")
        return df

    has_hist = df["形成期報酬(%)"].notna()
    df.loc[~has_hist, "未入選原因"] = REJECT_HISTORY

    # 成交額未知（NaN）時不當作流動性不足——那是資料問題，不是股票的問題。
    turnover = df["20日均成交額"]
    liquid = has_hist & (turnover.isna() | (turnover >= min_turnover))
    df.loc[has_hist & ~liquid, "未入選原因"] = REJECT_LIQUIDITY

    # 「前 30%」是在通過流動性的股票裡面排，不是全市場——
    # 否則一堆成交清淡的小型股會把門檻拉高或拉低。
    pool = df[liquid]
    if pool.empty:
        df["排名"] = pd.Series(dtype="Int64")
        return df
    cutoff = pool["形成期報酬(%)"].quantile(1 - top_fraction)
    winner = liquid & (df["形成期報酬(%)"] > 0) & (df["形成期報酬(%)"] >= cutoff)
    df.loc[liquid & ~winner, "未入選原因"] = REJECT_NOT_WINNER

    persistent = winner & (df["前一期報酬(%)"] > 0)
    df.loc[winner & ~persistent, "未入選原因"] = REJECT_NOT_PERSISTENT

    continuous = persistent & (df["連續性ID"] < 0)
    df.loc[persistent & ~continuous, "未入選原因"] = REJECT_DISCRETE

    calm = continuous & (df["最近一個月(%)"] <= max_recent_spike * 100)
    df.loc[continuous & ~calm, "未入選原因"] = REJECT_SPIKE

    passed = df[calm].sort_values(["連續性ID", "形成期報酬(%)"],
                                  ascending=[True, False])
    chosen = passed.head(top_n).index
    df.loc[chosen, "入選"] = True
    df.loc[calm & ~df.index.isin(chosen), "未入選原因"] = f"通過所有條件，但排在前 {top_n} 名之後"

    df["排名"] = pd.Series(dtype="Int64")
    df.loc[passed.index, "排名"] = range(1, len(passed) + 1)
    return df.sort_values(["入選", "排名"], ascending=[False, True], na_position="last")


def explain_row(r: pd.Series) -> str:
    """把一列指標翻成一句白話，給人看的。"""
    if pd.isna(r.get("形成期報酬(%)")):
        return REJECT_HISTORY
    s = (f"近半年 {r['形成期報酬(%)']:+.1f}%、前半年 {r['前一期報酬(%)']:+.1f}%、"
         f"上漲日 {r['上漲日比例(%)']:.0f}%、最近一個月 {r['最近一個月(%)']:+.1f}%")
    return s


# ----------------------------------------------------------------------
# 資料取得（需要網路，只在使用者機器上跑）
# ----------------------------------------------------------------------

def download_history(stock_ids: list[str], months: int = 15,
                     batch: int = 100) -> dict[str, pd.DataFrame]:
    """
    用 yfinance 批次下載日 K。回傳 {股票代碼: DataFrame(Open/High/Low/Close/Volume)}。

    一次丟幾百檔容易被限流，所以分批。抓不到的就略過，不讓一檔
    拖垮整批——那一檔會在結果裡標成「歷史資料不足」。
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  ✗ 未安裝 yfinance：pip install yfinance")
        return {}

    out: dict[str, pd.DataFrame] = {}
    ids = [str(s).strip() for s in stock_ids if str(s).strip()]
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        tickers = [f"{s}.TW" for s in chunk]
        print(f"    下載 {i + 1}~{i + len(chunk)} / {len(ids)} ...")
        try:
            raw = yf.download(tickers, period=f"{months}mo", auto_adjust=True,
                              group_by="ticker", threads=True, progress=False)
        except Exception as e:                                     # noqa: BLE001
            print(f"    ⚠ 這一批下載失敗：{type(e).__name__}: {e}")
            continue
        if raw is None or raw.empty:
            continue
        for sid, tk in zip(chunk, tickers):
            try:
                sub = raw[tk] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            sub = sub.dropna(how="all")
            if not sub.empty and "Close" in sub:
                out[sid] = sub
    return out


def screen(history: dict[str, pd.DataFrame], names: dict[str, str] | None = None,
           top_n: int = TOP_N) -> pd.DataFrame:
    """把下載好的歷史資料算成指標、排名。"""
    names = names or {}
    rows = []
    for sid, h in history.items():
        m = compute_metrics(h.get("Close"), h.get("Volume"), h.get("High"), h.get("Low"))
        row = {"股票代碼": sid, "股票名稱": names.get(sid, "")}
        if m is None:
            row.update({k: np.nan for k in
                        ("收盤價", "形成期報酬(%)", "前一期報酬(%)", "最近一個月(%)",
                         "上漲日比例(%)", "連續性ID", "20日均成交額", "ATR")})
            row["站上60日均線"] = False
        else:
            row.update(m)
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return rank_universe(pd.DataFrame(rows), top_n=top_n)


def load_universe_via_predictor() -> pd.DataFrame | None:
    """單獨執行時，借用活躍股篩選程式取得全市場普通股清單。"""
    import importlib.util
    cands = sorted(ROOT.glob("tw_active_stocks_predictor_v*.py"))
    if not cands:
        print("  ✗ 找不到 tw_active_stocks_predictor_v*.py，無法取得股票清單")
        return None
    path = cands[-1]
    spec = importlib.util.spec_from_file_location("predictor", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    print(f"  取得全市場清單（借用 {path.name}）...")
    panel = m.fetch_panel_data(m.LOOKBACK_DAYS)
    scored = m.compute_daily_scores(panel)
    return m.predict_next_day_activity(scored)


def prefilter(universe: pd.DataFrame, n: int = PREFILTER_N) -> pd.DataFrame:
    """
    先依最新成交額取前 n 檔，再去下載歷史。

    全市場九百多檔一次下載又慢又容易被限流；成交額排在幾百名以後的
    股票，本來就會被流動性門檻擋掉，先剔除不影響結果。
    """
    col = "latest_trading_money"
    if col in universe.columns:
        return universe.sort_values(col, ascending=False).head(n)
    return universe.head(n)


def save(result: pd.DataFrame, path: Path) -> None:
    chosen = result[result["入選"]].copy()
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        chosen.to_excel(w, sheet_name="入選名單", index=False)
        result.to_excel(w, sheet_name="全部候選與未入選原因", index=False)
        pd.DataFrame({
            "參數": ["跳過最近", "形成期", "贏家門檻", "成交額下限", "最近一個月漲幅上限",
                     "入選檔數", "篩選日期"],
            "值": [f"{SKIP_DAYS} 個交易日", f"{FORMATION_DAYS} 個交易日",
                   f"前 {TOP_FRACTION:.0%}", f"{MIN_AVG_TURNOVER:,.0f} 元",
                   f"{MAX_RECENT_SPIKE:.0%}", TOP_N, dt.date.today().isoformat()],
        }).to_excel(w, sheet_name="參數", index=False)


def print_result(result: pd.DataFrame, top_n: int) -> None:
    chosen = result[result["入選"]]
    if chosen.empty:
        print("\n  今天沒有股票同時通過所有條件。")
        print("  這不是錯誤——多頭不明顯時，「持續上漲」的股票本來就少。")
        reasons = result["未入選原因"].value_counts()
        print("\n  落選原因分布：")
        for r, c in reasons.items():
            print(f"    {c:>4} 檔  {r}")
        return
    print(f"\n  持續上漲前 {len(chosen)} 名（依上漲連續性排序）")
    print("  " + "-" * 66)
    for _, r in chosen.iterrows():
        print(f"  {int(r['排名']):>2}. {r['股票代碼']} {str(r['股票名稱'])[:8]:8s}  "
              f"{explain_row(r)}")
    print("\n  落選原因分布（全部候選）：")
    for reason, c in result.loc[~result["入選"], "未入選原因"].value_counts().items():
        print(f"    {c:>4} 檔  {reason}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="持續上漲股篩選")
    ap.add_argument("--top", type=int, default=TOP_N, help=f"入選檔數（預設 {TOP_N}）")
    ap.add_argument("--explain", metavar="代碼", help="說明某檔為什麼入選或落選")
    ap.add_argument("-o", "--output", help="輸出 Excel 路徑")
    args = ap.parse_args(argv)

    print("=" * 70)
    print("  持續上漲股篩選")
    print("=" * 70)
    universe = load_universe_via_predictor()
    if universe is None or universe.empty:
        print("  ✗ 取不到股票清單")
        return 1
    pool = prefilter(universe)
    names = dict(zip(pool["stock_id"].astype(str), pool["stock_name"]))
    print(f"  依成交額先取前 {len(pool)} 檔，下載約 15 個月歷史...")
    hist = download_history(pool["stock_id"].astype(str).tolist())
    print(f"  成功取得 {len(hist)} 檔")
    result = screen(hist, names, top_n=args.top)
    if result.empty:
        print("  ✗ 沒有可用的歷史資料")
        return 1

    if args.explain:
        r = result[result["股票代碼"].astype(str) == args.explain.strip()]
        if r.empty:
            print(f"\n  {args.explain} 不在候選池裡（可能成交額排在前 {PREFILTER_N} 名之外）")
        else:
            r = r.iloc[0]
            print(f"\n  {r['股票代碼']} {r['股票名稱']}")
            print(f"    {explain_row(r)}")
            print(f"    連續性ID {r['連續性ID']:+.3f}（越負越連續）")
            print("    結果：" + ("✓ 入選" if r["入選"] else f"✗ {r['未入選原因']}"))
        return 0

    print_result(result, args.top)
    out = Path(args.output) if args.output else ROOT / f"趨勢股篩選結果_{dt.date.today().isoformat()}.xlsx"
    save(result, out)
    print(f"\n  完整結果：{out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
