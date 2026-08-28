# -*- coding: utf-8 -*-
"""
stock_analysis_log.xlsx 回測驗證工具 v1.1
==========================================
v1.1：支援 claude_stock_analyzer v3.7 新增的欄位。若 log 帶有「是否盤中執行」
      欄位就直接採用（v3.7 會誠實記錄），沒有的話才退回用執行時刻推斷（v3.6
      以前的舊紀錄）。新增 --write-back，把實際結果直接回填進 log 檔本身。
用途：把 claude_stock_analyzer 累積的長期記錄，變成「這套系統到底準不準」
      的量化答案。analyzer 目前只記錄「當下的預測」，從來沒有回填「後來
      實際發生了什麼」，所以無法自我驗證——這支程式補上這一塊。

它做三件事：
    1. 資料品質稽核：找出盤中執行(未完成K棒)、資料停滯、關鍵欄位缺漏、
       同一預測重複記錄等會汙染統計的紀錄。
    2. 回填實際結果：取得「預測目標日」的真實收盤價，算出實際漲跌，
       標記每一筆預測是否命中。
       - online 模式：用 yfinance 抓真實收盤價（需要能連 Yahoo Finance）。
       - offline 模式：只用 log 自己記錄過的收盤價互相對照（不需網路，
         但只能驗證「後來又有跑到同一檔」的那些預測）。
    3. 產出評估報告：命中率、機率校準表、期望值、與「全部猜漲/全部猜跌」
       等笨基準的比較，並輸出成 Excel。

使用方式：
    python tools/log_review.py stock_analysis_log.xlsx                 # 自動：能連網就 online
    python tools/log_review.py stock_analysis_log.xlsx --mode offline  # 強制離線
    python tools/log_review.py stock_analysis_log.xlsx -o report.xlsx
    python tools/log_review.py stock_analysis_log.xlsx --write-back    # 把實際結果回填進 log

重要限制（請務必理解，不要拿小樣本當結論）：
    要證明「準確率 55%」顯著優於「隨機 50%」，在 95% 信心水準下大約需要
    380 筆以上的獨立預測。而且同一天不同股票的漲跌高度相關（大盤一動全動），
    所以 10 檔股票 × 1 天，有效樣本數比較接近 1 天而不是 10 筆。
    本工具會印出樣本數與標準誤，請照著它判讀，不要看到命中率高就當真。
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# 參數
# ============================================================

SHEET_NAME = "分析紀錄"

# 台股連續交易時段 09:00–13:30。這個區間內執行，yfinance 回傳的「當日」
# K 棒尚未收盤，Close 其實是當下成交價，特徵值會跟訓練時看到的完整日 K
# 分布不同 → 該筆預測不可信，統計時必須排除。
SESSION_OPEN = dt.time(9, 0)
SESSION_CLOSE = dt.time(13, 30)
# 收盤後留一段緩衝，等資料源更新完畢才視為可信。
POST_CLOSE_SAFE = dt.time(14, 0)

COL_TS = "執行時間"
COL_SYMBOL = "股票代碼"
COL_BASIS = "股價日期(資料基準日)"
COL_TARGET = "預測目標日(隔日估計)"
COL_PRICE = "目前股價"
COL_LR = "隔日_邏輯迴歸_上漲機率(%)"
COL_RF = "隔日_RF_上漲機率(%)"
COL_LR_ACC = "隔日_邏輯迴歸_樣本外準確率(%)"
COL_RF_ACC = "隔日_RF_樣本外準確率(%)"
COL_SCORE = "綜合分數"
COL_DECISION = "Strategy_Decision"
COL_QUALITY = "model_quality"
# v3.7 新增欄位（舊紀錄沒有這些欄位時，程式會自動退回舊的推斷方式）
COL_INTRADAY_FLAG = "是否盤中執行"
COL_STALE_FLAG = "資料是否停滯"
COL_LR_N = "隔日_邏輯迴歸_樣本數"
COL_RF_N = "隔日_RF_樣本數"
COL_LR_LB = "隔日_邏輯迴歸_準確率信賴下限(%)"
COL_RF_LB = "隔日_RF_準確率信賴下限(%)"
COL_EV_DECISION = "EV_Decision"

# 回填欄位（v3.7 的 EXCEL_LOG_COLUMNS 已預留；舊檔案會由 --write-back 自動補上）
BACKFILL_COLUMNS = [
    "實際目標日收盤", "實際報酬(%)", "實際方向",
    "是否命中_邏輯迴歸", "是否命中_RF", "是否命中_綜合分數", "回填時間",
]


# ============================================================
# 載入與正規化
# ============================================================

def load_log(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=SHEET_NAME)
    if "完整終端輸出" in df.columns:
        df = df.drop(columns=["完整終端輸出"])  # 太大，統計用不到

    df[COL_TS] = pd.to_datetime(df[COL_TS], errors="coerce")
    df["基準日"] = pd.to_datetime(df[COL_BASIS], errors="coerce").dt.date
    df["目標日"] = pd.to_datetime(df[COL_TARGET], errors="coerce").dt.date
    df["執行日"] = df[COL_TS].dt.date
    df["執行時刻"] = df[COL_TS].dt.time

    # 盤中與否：v3.7 會誠實記錄「是否盤中執行」，直接採用；舊紀錄沒有這欄，
    # 才退回用執行時刻推斷（台股 09:00–13:30）。
    inferred_intraday = df["執行時刻"].apply(
        lambda t: SESSION_OPEN <= t <= SESSION_CLOSE if pd.notna(t) else False
    )
    if COL_INTRADAY_FLAG in df.columns:
        flag = df[COL_INTRADAY_FLAG]
        df["盤中執行"] = flag.where(flag.notna(), inferred_intraday).astype(bool)
        df["盤中判定來源"] = np.where(flag.notna(), "程式記錄(v3.7)", "執行時刻推斷")
    else:
        df["盤中執行"] = inferred_intraday
        df["盤中判定來源"] = "執行時刻推斷"

    # v3.7 起，盤中執行時已剔除未完成K棒，所以那些紀錄其實是可信的；
    # 但它們的資料基準日會是前一交易日，去重時仍統一以每個(股票,基準日)
    # 的最後一筆為準，不需要因為盤中就整筆排除。
    trimmed_ok = (
        df.get("已剔除未完成K棒", pd.Series(False, index=df.index)).fillna(False).astype(bool)
        if "已剔除未完成K棒" in df.columns else pd.Series(False, index=df.index)
    )
    by_time = df["執行時刻"].apply(
        lambda t: (t < SESSION_OPEN or t >= POST_CLOSE_SAFE) if pd.notna(t) else False
    )
    df["資料可信"] = by_time | trimmed_ok

    # 資料停滯或被跳過的紀錄一律不納入統計
    if COL_STALE_FLAG in df.columns:
        df.loc[df[COL_STALE_FLAG].fillna(False).astype(bool), "資料可信"] = False
    if COL_DECISION in df.columns:
        skipped = df[COL_DECISION].astype(str).str.startswith("Skipped")
        df.loc[skipped, "資料可信"] = False
    return df


# ============================================================
# 1. 資料品質稽核
# ============================================================

def audit(df: pd.DataFrame) -> dict:
    """回傳各類問題紀錄，key 是問題名稱，value 是 DataFrame。"""
    issues: dict[str, pd.DataFrame] = {}

    intraday = df[df["盤中執行"]]
    if len(intraday):
        issues["盤中執行(未完成K棒)"] = intraday[
            [COL_TS, COL_SYMBOL, COL_BASIS, COL_PRICE, COL_LR, COL_RF]
        ]

    # 資料停滯：執行日已經比資料基準日晚超過一個週末，代表這檔的價格資料
    # 沒更新到最新交易日，但程式照樣輸出了機率。
    def _stale_days(row):
        if pd.isna(row["執行日"]) or pd.isna(row["基準日"]):
            return np.nan
        return (row["執行日"] - row["基準日"]).days

    df = df.copy()
    df["資料落後天數"] = df.apply(_stale_days, axis=1)
    stale = df[df["資料落後天數"] > 3]
    if len(stale):
        issues["資料停滯(價格未更新)"] = stale[
            [COL_TS, COL_SYMBOL, COL_BASIS, COL_PRICE, "資料落後天數"]
        ]

    missing = df[df[COL_LR].isna() & df[COL_RF].isna()]
    if len(missing):
        issues["模型無輸出仍寫入紀錄"] = missing[
            [COL_TS, COL_SYMBOL, "市場狀態", COL_SCORE, COL_QUALITY]
        ]

    no_price = df[df[COL_PRICE].isna()]
    if len(no_price):
        issues["股價缺失仍寫入紀錄"] = no_price[[COL_TS, COL_SYMBOL, COL_BASIS, COL_SCORE]]

    # 同一(股票, 基準日)被記錄多次 → 統計時若不去重，同一個預測會被算好幾遍
    dup = (
        df.groupby([COL_SYMBOL, "基準日"])
        .size()
        .reset_index(name="重複次數")
        .query("重複次數 > 1")
        .sort_values("重複次數", ascending=False)
    )
    if len(dup):
        issues["同一預測重複記錄"] = dup

    # 同一(股票, 基準日)在不同執行時間得到差異很大的機率 → 不穩定
    rows = []
    for (sid, basis), g in df.dropna(subset=[COL_LR]).groupby([COL_SYMBOL, "基準日"]):
        if len(g) < 2:
            continue
        lr, rf = g[COL_LR], g[COL_RF]
        rows.append(
            {
                COL_SYMBOL: sid,
                "基準日": basis,
                "執行次數": len(g),
                "執行時刻": ", ".join(str(t)[:5] for t in g["執行時刻"]),
                "邏輯迴歸機率": ", ".join(f"{v:.1f}" for v in lr),
                "LR極差(pp)": float(lr.max() - lr.min()),
                "RF極差(pp)": float(rf.max() - rf.min()) if rf.notna().any() else np.nan,
                "含盤中執行": bool(g["盤中執行"].any()),
            }
        )
    if rows:
        issues["同日多次執行機率不穩定"] = (
            pd.DataFrame(rows).sort_values("LR極差(pp)", ascending=False)
        )

    return issues


# ============================================================
# 2. 回填實際結果
# ============================================================

def build_price_map_offline(df: pd.DataFrame) -> pd.Series:
    """
    只用 log 自身資料建立 (股票, 日期) -> 收盤價 的對照表。
    僅採用「資料可信」(盤前或收盤後) 的紀錄，因為盤中紀錄的股價不是收盤價。
    """
    ok = df[df["資料可信"] & df[COL_PRICE].notna()]
    return ok.groupby([COL_SYMBOL, "基準日"])[COL_PRICE].last()


def build_price_map_online(symbols, start: dt.date, end: dt.date) -> pd.Series:
    """用 yfinance 抓真實收盤價。抓不到的標的只會少那幾檔，不中斷整體流程。"""
    try:
        import yfinance as yf
    except ImportError:
        print("  ⚠ 未安裝 yfinance，改用 offline 模式 (pip install yfinance)")
        return pd.Series(dtype=float)

    records = {}
    for sid in sorted(set(symbols)):
        try:
            hist = yf.Ticker(str(sid)).history(
                start=start - dt.timedelta(days=7),
                end=end + dt.timedelta(days=7),
                auto_adjust=True,
            )
            if hist.empty:
                print(f"  ⚠ {sid}: 查無資料，略過")
                continue
            for idx, row in hist.iterrows():
                records[(sid, idx.date())] = float(row["Close"])
            print(f"  ✓ {sid}: 取得 {len(hist)} 個交易日")
        except Exception as e:  # 單檔失敗不影響其他標的
            print(f"  ⚠ {sid}: 抓取失敗 {type(e).__name__}: {e}")

    if not records:
        return pd.Series(dtype=float)
    s = pd.Series(records)
    s.index = pd.MultiIndex.from_tuples(s.index, names=[COL_SYMBOL, "基準日"])
    return s


def backfill(df: pd.DataFrame, price_map: pd.Series) -> pd.DataFrame:
    """
    每個 (股票, 基準日) 只保留最後一筆「資料可信」的預測，然後回填
    基準日收盤、目標日收盤、實際報酬與命中與否。
    """
    df = df.copy()
    if "_原始列號" not in df.columns:
        df["_原始列號"] = np.arange(len(df))
    valid = df[df["資料可信"]].sort_values(COL_TS)
    dedup = valid.groupby([COL_SYMBOL, "基準日"], as_index=False).last()

    out = []
    for _, r in dedup.iterrows():
        c0 = price_map.get((r[COL_SYMBOL], r["基準日"]), np.nan)
        c1 = price_map.get((r[COL_SYMBOL], r["目標日"]), np.nan)
        if pd.isna(c0) or pd.isna(c1) or c0 == 0:
            continue
        ret = (c1 - c0) / c0 * 100
        rec = {
            "股票代碼": r[COL_SYMBOL],
            "基準日": r["基準日"],
            "目標日": r["目標日"],
            "基準日收盤": c0,
            "目標日收盤": c1,
            "實際報酬(%)": ret,
            "實際方向": "漲" if ret > 0 else ("跌" if ret < 0 else "平"),
            "LR上漲機率(%)": r.get(COL_LR),
            "RF上漲機率(%)": r.get(COL_RF),
            "LR樣本外準確率(%)": r.get(COL_LR_ACC),
            "RF樣本外準確率(%)": r.get(COL_RF_ACC),
            "綜合分數": r.get(COL_SCORE),
            "Strategy_Decision": r.get(COL_DECISION),
            "EV_Decision": r.get(COL_EV_DECISION),
            "model_quality": r.get(COL_QUALITY),
            "LR樣本數": r.get(COL_LR_N),
            "RF樣本數": r.get(COL_RF_N),
            "LR準確率信賴下限(%)": r.get(COL_LR_LB),
            "RF準確率信賴下限(%)": r.get(COL_RF_LB),
            "_原始列號": r.get("_原始列號"),
        }
        for key, col in (("LR", COL_LR), ("RF", COL_RF)):
            p = r.get(col)
            rec[f"{key}預測方向"] = (
                np.nan if pd.isna(p) else ("漲" if p >= 50 else "跌")
            )
            rec[f"{key}命中"] = (
                np.nan if pd.isna(p) else int((p >= 50) == (ret > 0))
            )
        sc = r.get(COL_SCORE)
        rec["綜合分數命中"] = np.nan if pd.isna(sc) else int((sc > 0) == (ret > 0))
        out.append(rec)

    return pd.DataFrame(out)


# ============================================================
# 3. 評估
# ============================================================

def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 信賴區間，小樣本比常態近似可靠。"""
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, (c - h) * 100), min(100.0, (c + h) * 100))


def hit_rate_table(ev: pd.DataFrame) -> pd.DataFrame:
    up = ev["實際報酬(%)"] > 0
    rows = []

    def add(name, mask_pred, mask_valid):
        n = int(mask_valid.sum())
        if n == 0:
            return
        k = int((mask_pred[mask_valid] == up[mask_valid]).sum())
        lo, hi = _wilson(k, n)
        rows.append(
            {
                "策略": name,
                "樣本數": n,
                "命中": k,
                "命中率(%)": k / n * 100,
                "95%信賴區間下限": lo,
                "95%信賴區間上限": hi,
                "是否顯著優於50%": "是" if lo > 50 else "否",
            }
        )

    add("邏輯迴歸 (機率≥50%看漲)", ev["LR上漲機率(%)"] >= 50, ev["LR上漲機率(%)"].notna())
    add("Random Forest (機率≥50%看漲)", ev["RF上漲機率(%)"] >= 50, ev["RF上漲機率(%)"].notna())
    add("綜合分數 (>0看漲)", ev["綜合分數"] > 0, ev["綜合分數"].notna())
    # v3.7 的兩套決策規則實際表現比較：這正是採用影子決策的目的——
    # 讓資料決定要不要把 Strategy_Decision 換成 EV_Decision。
    for name, col in (("Strategy_Decision", "Strategy_Decision"),
                      ("EV_Decision (v3.7影子)", "EV_Decision")):
        if col in ev.columns:
            acted = ev[col].isin(["Buy", "Sell"])
            if acted.any():
                add(f"{name} 有出手時", ev[col] == "Buy", acted)
    all_true = pd.Series(True, index=ev.index)
    add("笨基準：全部猜漲", all_true, all_true)
    add("笨基準：全部猜跌", ~all_true, all_true)
    return pd.DataFrame(rows)


def calibration_table(ev: pd.DataFrame, col: str = "LR上漲機率(%)") -> pd.DataFrame:
    """
    機率校準：模型說 70% 會漲的那些日子，實際上真的漲了幾成？
    一個可用的模型，這兩個數字應該貼近；差很多代表機率值不能當信心用。
    """
    sub = ev.dropna(subset=[col])
    if sub.empty:
        return pd.DataFrame()
    bins = [0, 40, 45, 50, 55, 60, 70, 100]
    labels = ["<40", "40-45", "45-50", "50-55", "55-60", "60-70", "≥70"]
    g = sub.groupby(pd.cut(sub[col], bins=bins, labels=labels, right=False), observed=True)
    out = g.apply(
        lambda x: pd.Series(
            {
                "樣本數": len(x),
                "模型平均機率(%)": x[col].mean(),
                "實際上漲比例(%)": (x["實際報酬(%)"] > 0).mean() * 100,
                "平均隔日報酬(%)": x["實際報酬(%)"].mean(),
            }
        ),
        include_groups=False,
    ).reset_index()
    out = out.rename(columns={out.columns[0]: "機率區間(%)"})
    out["校準落差(pp)"] = out["實際上漲比例(%)"] - out["模型平均機率(%)"]
    return out


def expectancy(ev: pd.DataFrame, col: str = "LR上漲機率(%)", threshold: float = 50.0) -> dict:
    """
    若照模型方向做多/做空，扣掉台股來回交易成本後的期望值。
    成本假設：手續費 0.1425%×6折×2 + 證交稅 0.3% = 約 0.47%（不含滑價）。
    """
    cost_pct = 0.1425 * 0.6 * 2 + 0.3
    sub = ev.dropna(subset=[col])
    sub = sub[(sub[col] - 50).abs() >= (threshold - 50)]
    if sub.empty:
        return {}
    sign = np.where(sub[col] >= 50, 1, -1)
    gross = sign * sub["實際報酬(%)"].values
    net = gross - cost_pct
    wins, losses = net[net > 0], net[net < 0]
    return {
        "訊號門檻(%)": threshold,
        "交易次數": len(net),
        "勝率(%)": float((net > 0).mean() * 100),
        "平均獲利(%)": float(wins.mean()) if len(wins) else np.nan,
        "平均虧損(%)": float(-losses.mean()) if len(losses) else np.nan,
        "毛期望值(%)": float(gross.mean()),
        "假設來回成本(%)": cost_pct,
        "淨期望值(%)": float(net.mean()),
        "淨期望值標準誤(%)": float(net.std(ddof=1) / math.sqrt(len(net))) if len(net) > 1 else np.nan,
    }


def power_note(n: int) -> str:
    if n == 0:
        return "沒有可驗證樣本。"
    se = math.sqrt(0.25 / n) * 100
    need = int(0.25 * (1.96 / 0.05) ** 2)
    return (
        f"目前可驗證樣本 n={n}，命中率的 1 個標準誤約 ±{se:.1f}pp。\n"
        f"  要在 95% 信心下證明「命中率 55%」真的優於隨機的 50%，大約需要 n≥{need}。\n"
        f"  另外，同一天不同股票的漲跌高度相關，有效獨立樣本數接近「交易日數」而非「筆數」。"
    )


# ============================================================
# 報告輸出
# ============================================================

def print_report(df: pd.DataFrame, issues: dict, ev: pd.DataFrame, mode: str) -> None:
    line = "=" * 68
    print(f"\n{line}\n  stock_analysis_log 驗證報告（回填模式：{mode}）\n{line}")

    print(f"\n【紀錄概況】")
    print(f"  總筆數           : {len(df)}")
    if df[COL_TS].notna().any():
        print(f"  期間             : {df[COL_TS].min()} ～ {df[COL_TS].max()}")
    print(f"  涵蓋標的         : {df[COL_SYMBOL].nunique()} 檔")
    print(f"  資料可信筆數     : {int(df['資料可信'].sum())}"
          f"（盤中執行 {int(df['盤中執行'].sum())} 筆已排除）")
    if COL_DECISION in df:
        vc = df[COL_DECISION].value_counts(dropna=False).to_dict()
        print(f"  Strategy_Decision: {vc}")
    if COL_QUALITY in df:
        print(f"  model_quality    : {df[COL_QUALITY].value_counts(dropna=False).to_dict()}")

    print(f"\n【資料品質稽核】")
    if not issues:
        print("  未發現問題。")
    for name, tbl in issues.items():
        print(f"\n  ▸ {name}：{len(tbl)} 筆")
        print(tbl.head(12).to_string(index=False))
        if len(tbl) > 12:
            print(f"    ...（另有 {len(tbl) - 12} 筆，詳見輸出 Excel）")

    if ev.empty:
        print("\n【預測驗證】無法回填任何實際結果。")
        print("  offline 模式只能驗證『同一檔在目標日又被跑過一次』的預測；")
        print("  想要完整驗證，請在能連 Yahoo Finance 的環境用 --mode online。")
        return

    print(f"\n【預測驗證】(n={len(ev)})")
    show = [
        "股票代碼", "基準日", "目標日", "基準日收盤", "目標日收盤",
        "實際報酬(%)", "實際方向", "LR上漲機率(%)", "RF上漲機率(%)", "綜合分數",
    ]
    print(ev[show].round(2).to_string(index=False))

    print(f"\n【命中率】")
    print(hit_rate_table(ev).round(2).to_string(index=False))

    cal = calibration_table(ev)
    if not cal.empty:
        print(f"\n【邏輯迴歸機率校準】(模型說幾成會漲 vs 實際漲了幾成)")
        print(cal.round(2).to_string(index=False))

    print(f"\n【期望值(已扣交易成本)】")
    for th in (50.0, 55.0, 58.0, 60.0):
        e = expectancy(ev, threshold=th)
        if e:
            print(f"  門檻{th:.0f}%: 交易{e['交易次數']:3d}次 勝率{e['勝率(%)']:.1f}% "
                  f"毛EV {e['毛期望值(%)']:+.2f}% 淨EV {e['淨期望值(%)']:+.2f}% "
                  f"(±{e['淨期望值標準誤(%)']:.2f}%)")

    print(f"\n【統計檢定力提醒】\n  {power_note(len(ev))}")
    print(f"\n{line}\n免責聲明：本報告是對既有紀錄的統計整理，不構成投資建議。\n{line}\n")


def write_back(log_path: Path, ev: pd.DataFrame) -> int:
    """
    把實際結果直接回填進 log 檔案本身（v3.7 的 EXCEL_LOG_COLUMNS 已預留這些
    欄位；舊檔案會自動補上）。

    這是整套流程真正的閉環：analyzer 負責記錄「當時怎麼判斷」，這裡負責補上
    「後來實際發生什麼」。沒有這一步，跑再多筆紀錄都只是在累積無法驗證的數字。

    只回填空白的欄位，不覆蓋已有的值，所以可以每天重複執行。
    回傳實際寫入的列數。
    """
    if ev.empty or "_原始列號" not in ev.columns:
        print("  沒有可回填的資料。")
        return 0

    book = pd.read_excel(log_path, sheet_name=SHEET_NAME)
    # 以 object dtype 建立/轉換，避免把「漲/跌」這種字串寫進 float 欄位時
    # 被 pandas 3.0 的 dtype 檢查擋下。
    for c in BACKFILL_COLUMNS:
        if c not in book.columns:
            book[c] = pd.Series([pd.NA] * len(book), dtype="object")
        else:
            book[c] = book[c].astype("object")

    # 來源欄位 → log 欄位
    field_map = {
        "實際目標日收盤": "目標日收盤",
        "實際報酬(%)": "實際報酬(%)",
        "實際方向": "實際方向",
        "是否命中_邏輯迴歸": "LR命中",
        "是否命中_RF": "RF命中",
        "是否命中_綜合分數": "綜合分數命中",
    }

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates = {c: {} for c in BACKFILL_COLUMNS}
    written = 0
    already = book["實際報酬(%)"].notna()

    for _, r in ev.iterrows():
        pos = r.get("_原始列號")
        if pd.isna(pos):
            continue
        pos = int(pos)
        if pos >= len(book) or already.iloc[pos]:
            continue  # 超出範圍，或已回填過（不覆蓋既有值）
        for log_col, src_col in field_map.items():
            val = r.get(src_col)
            updates[log_col][pos] = None if pd.isna(val) else val
        updates["回填時間"][pos] = now
        written += 1

    if written:
        for col, mapping in updates.items():
            if not mapping:
                continue
            series = book[col].copy()
            for pos, val in mapping.items():
                series.iat[pos] = val
            book[col] = series
        with pd.ExcelWriter(log_path, engine="openpyxl", mode="w") as w:
            book.to_excel(w, sheet_name=SHEET_NAME, index=False)
        print(f"  ✓ 已回填 {written} 列實際結果至 {log_path}")
    else:
        print("  沒有需要回填的列（可能都已回填過，或尚無可驗證的實際結果）。")
    return written


def write_excel(path: Path, df, issues, ev) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        if not ev.empty:
            ev.to_excel(w, sheet_name="回填驗證明細", index=False)
            hit_rate_table(ev).to_excel(w, sheet_name="命中率", index=False)
            cal = calibration_table(ev)
            if not cal.empty:
                cal.to_excel(w, sheet_name="機率校準", index=False)
            rows = [expectancy(ev, threshold=t) for t in (50.0, 55.0, 58.0, 60.0)]
            pd.DataFrame([r for r in rows if r]).to_excel(w, sheet_name="期望值", index=False)
        for name, tbl in issues.items():
            tbl.to_excel(w, sheet_name=name[:31], index=False)
        if not issues and ev.empty:
            pd.DataFrame({"訊息": ["無資料"]}).to_excel(w, sheet_name="說明", index=False)
    print(f"報告已輸出：{path}")


# ============================================================
# 主流程
# ============================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="stock_analysis_log.xlsx 回測驗證工具")
    ap.add_argument("log", type=Path, help="stock_analysis_log.xlsx 路徑")
    ap.add_argument("--mode", choices=["auto", "online", "offline"], default="auto",
                    help="回填實際收盤價的方式（預設 auto：先試 online，失敗退回 offline）")
    ap.add_argument("-o", "--output", type=Path, default=None, help="輸出 Excel 報告路徑")
    ap.add_argument("--write-back", action="store_true",
                    help="把實際結果回填進 log 檔案本身（只補空白欄位，不覆蓋既有值）")
    args = ap.parse_args(argv)

    if not args.log.exists():
        print(f"找不到檔案：{args.log}")
        return 1

    df = load_log(args.log)
    issues = audit(df)

    price_map = pd.Series(dtype=float)
    used_mode = "offline"
    if args.mode in ("auto", "online"):
        print("[回填] 嘗試以 yfinance 取得真實收盤價...")
        dates = df["基準日"].dropna()
        if len(dates):
            price_map = build_price_map_online(
                df[COL_SYMBOL].dropna().unique(), dates.min(), df["目標日"].dropna().max()
            )
        if len(price_map):
            used_mode = "online"
        elif args.mode == "online":
            print("  ✗ online 回填失敗（可能是網路被擋）。")
            return 2
        else:
            print("  → 退回 offline 模式：改用 log 自身記錄過的收盤價。")

    if used_mode == "offline":
        price_map = build_price_map_offline(df)

    ev = backfill(df, price_map)
    print_report(df, issues, ev, used_mode)

    if args.write_back:
        print("\n[回填] 寫回 log 檔案...")
        try:
            write_back(args.log, ev)
        except PermissionError:
            print(f"  ⚠ 無法寫入 {args.log}（檔案可能正在 Excel 中開啟，請先關閉再執行）")

    out = args.output or args.log.with_name(
        f"log_review_{dt.date.today().isoformat()}.xlsx"
    )
    write_excel(out, df, issues, ev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
