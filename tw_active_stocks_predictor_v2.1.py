# -*- coding: utf-8 -*-
"""
台股活躍股預測程式 v2.1
========================
v1.0 -> v2.0 變更說明：
    v1.0 使用 FinMind 的「全市場單日資料」功能，實際測試後發現該功能
    是付費會員限定（免費帳號會噴 Exception: Your level is free...），
    因此 v2.0 改用「證交所官方公開 API」，完全免費、免註冊、免 Token。

⚠️ 本版範圍限制（請確實理解後再使用）：
    1. 只涵蓋「上市 (TWSE)」股票，不含上櫃 (TPEx) 與興櫃。
       上櫃的官方端點格式較不穩定，為避免猜測導致錯誤，暫不支援。
       若之後要擴充上櫃，需要另外開發並測試。
    2. 沒有「換手率」（需要股本資料，公開 API 未提供），
       改用「成交筆數」作為周轉活絡度的替代指標，僅供參考。
    3.「預測明日活躍度」是用近 N 日活躍度分數做線性趨勢外推（動能延伸法），
       *不是* 機器學習模型，準確度僅供參考，不構成任何投資建議。
    4. 本程式需在你本機（VS Code）執行，Claude 的 sandbox 無法連線
       twse.com.tw。
    5. 證交所網站對爬蟲行為可能有基本的頻率限制，程式已加入延遲，
       若你之後想提高抓取頻率，請自行斟酌調整 time.sleep()。

版本紀錄：
    v1.0 (2026-08-25) 初版，使用 FinMind（後來發現全市場端點需付費，已棄用）
    v2.0 (2026-08-25) 改用證交所官方免費 API，僅涵蓋上市股票
    v2.1 (2026-08-25) 修正證交所回應格式解析：改為 {"tables":[{fields,data},...]}
                       巢狀結構（v2.0 誤用舊版扁平 fields9/data9 結構，抓不到資料）
"""

import sys
import time
import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ============================================================
# 參數設定區（可依需求調整）
# ============================================================

LOOKBACK_DAYS = 5  # 取近幾個交易日做活躍度分析
TOP_N = 10  # 輸出前幾名

# 活躍度綜合分數權重（四項標準化後加權加總，總和建議 = 1.0）
WEIGHT_TRADING_MONEY = 0.35  # 成交值
WEIGHT_VOLUME_RATIO = 0.30  # 成交量變化率（今日量 / 前幾日均量）
WEIGHT_TURNOVER_COUNT = 0.20  # 成交筆數（換手率替代指標）
WEIGHT_AMPLITUDE = 0.15  # 當日振幅

OUTPUT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = OUTPUT_DIR / f"台股活躍股預測_{datetime.date.today().isoformat()}.xlsx"

TWSE_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

NEEDED_COLUMNS = {
    "證券代號": "stock_id",
    "證券名稱": "stock_name",
    "成交股數": "Trading_Volume",
    "成交筆數": "Trading_turnover",
    "成交金額": "Trading_money",
    "開盤價": "open",
    "最高價": "max",
    "最低價": "min",
    "收盤價": "close",
}


# ============================================================
# 資料抓取（證交所官方公開 API）
# ============================================================

def fetch_twse_day(date_str: str) -> pd.DataFrame:
    """
    抓取指定日期（YYYYMMDD）全部上市股票的日成交資料。
    若當天是假日或無資料，回傳 None。
    """
    params = {"response": "json", "date": date_str, "type": "ALLBUT0999"}
    try:
        resp = requests.get(TWSE_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    {date_str} 抓取失敗：{e}")
        return None

    if data.get("stat") != "OK":
        return None  # 假日或無交易資料

    # 2026年版證交所回應改為巢狀結構：{"tables": [{"title":..,"fields":[...],"data":[...]}, ...]}
    # 從 tables 清單中找出「fields 含證券代號」的那一張表（個股收盤行情表）
    tables = data.get("tables", [])
    target_table = None
    for t in tables:
        if isinstance(t, dict) and "證券代號" in t.get("fields", []):
            target_table = t
            break
    if target_table is None:
        return None

    rows = target_table.get("data", [])
    if not rows:
        return None

    df = pd.DataFrame(rows, columns=target_table["fields"])

    # 只保留有對應到需求欄位的資料，並清理數值格式
    missing = [c for c in NEEDED_COLUMNS if c not in df.columns]
    if missing:
        print(f"    警告：{date_str} 缺少欄位 {missing}，此日資料略過")
        return None

    df = df[list(NEEDED_COLUMNS.keys())].rename(columns=NEEDED_COLUMNS)

    numeric_cols = [
        "Trading_Volume",
        "Trading_turnover",
        "Trading_money",
        "open",
        "max",
        "min",
        "close",
    ]
    for col in numeric_cols:
        df[col] = (
            df[col]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.strip()
            .replace({"--": np.nan, "": np.nan, "---": np.nan})
        )
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["date"] = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    return df.reset_index(drop=True)


def fetch_panel_data(n_days: int) -> pd.DataFrame:
    """從昨天開始往前找，直到湊滿 n_days 個有效交易日的資料"""
    frames = []
    current = datetime.date.today() - datetime.timedelta(days=1)
    attempts = 0
    max_attempts = n_days * 3 + 15  # 預留假日緩衝

    while len(frames) < n_days and attempts < max_attempts:
        attempts += 1
        if current.weekday() < 5:  # 只嘗試週一到週五
            date_str = current.strftime("%Y%m%d")
            print(f"  嘗試抓取 {date_str} ...")
            df = fetch_twse_day(date_str)
            if df is not None:
                print(f"    OK，取得 {len(df)} 檔股票資料")
                frames.append(df)
            else:
                print(f"    無資料（可能為假日），略過")
            time.sleep(1.0)  # 放慢節奏，避免對證交所造成負擔
        current -= datetime.timedelta(days=1)

    if len(frames) < n_days:
        print(f"  警告：只湊到 {len(frames)} 個交易日（原訂 {n_days} 天），將以實際取得天數繼續分析")

    if not frames:
        raise RuntimeError("完全抓不到任何交易日資料，請檢查網路連線或證交所服務狀態")

    frames = list(reversed(frames))  # 依日期由舊到新排序
    return pd.concat(frames, ignore_index=True)


# ============================================================
# 活躍度計算
# ============================================================

def zscore(s: pd.Series) -> pd.Series:
    """標準化：(x - mean) / std，std=0 時回傳全 0"""
    std = s.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series(0, index=s.index)
    return (s - s.mean()) / std


def compute_daily_scores(panel: pd.DataFrame) -> pd.DataFrame:
    """
    對每個交易日，計算當天每檔股票的四項原始指標，
    並在該日的股票截面上做標準化，加權合成當日活躍度分數。
    """
    df = panel.copy()

    mid = (df["max"] + df["min"]) / 2
    mid = mid.replace(0, np.nan)
    df["amplitude"] = (df["max"] - df["min"]) / mid
    df["amplitude"] = df["amplitude"].fillna(0)

    df = df.sort_values(["stock_id", "date"])
    df["volume_ma_prior"] = (
        df.groupby("stock_id")["Trading_Volume"]
        .transform(lambda s: s.shift(1).expanding().mean())
    )
    df["volume_ratio"] = df["Trading_Volume"] / df["volume_ma_prior"]
    df["volume_ratio"] = df["volume_ratio"].replace([np.inf, -np.inf], np.nan)
    df["volume_ratio"] = df["volume_ratio"].fillna(1.0)  # 第一天無比較基準，補 1（持平）

    scored_frames = []
    for d, day_df in df.groupby("date"):
        day_df = day_df.copy()
        day_df["z_money"] = zscore(day_df["Trading_money"])
        day_df["z_volume_ratio"] = zscore(day_df["volume_ratio"])
        day_df["z_turnover_count"] = zscore(day_df["Trading_turnover"])
        day_df["z_amplitude"] = zscore(day_df["amplitude"])

        day_df["activity_score"] = (
            WEIGHT_TRADING_MONEY * day_df["z_money"]
            + WEIGHT_VOLUME_RATIO * day_df["z_volume_ratio"]
            + WEIGHT_TURNOVER_COUNT * day_df["z_turnover_count"]
            + WEIGHT_AMPLITUDE * day_df["z_amplitude"]
        )
        scored_frames.append(day_df)

    return pd.concat(scored_frames, ignore_index=True)


def predict_next_day_activity(scored: pd.DataFrame) -> pd.DataFrame:
    """
    對每檔股票，用近 N 日的活躍度分數做一次線性回歸（動能外推），
    估算「明日活躍度分數」= 最新分數 + 趨勢斜率。
    """
    results = []
    for stock_id, g in scored.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < 2:
            continue
        x = np.arange(len(g))
        y = g["activity_score"].values
        slope, intercept = np.polyfit(x, y, 1)
        latest_score = y[-1]
        predicted_score = latest_score + slope

        results.append(
            {
                "stock_id": stock_id,
                "stock_name": g["stock_name"].iloc[-1],
                "latest_score": latest_score,
                "trend_slope": slope,
                "predicted_next_score": predicted_score,
                "days_used": len(g),
                "latest_trading_money": g["Trading_money"].iloc[-1],
                "latest_volume": g["Trading_Volume"].iloc[-1],
                "latest_volume_ratio": g["volume_ratio"].iloc[-1],
                "latest_amplitude": g["amplitude"].iloc[-1],
                "latest_date": g["date"].iloc[-1],
            }
        )
    return pd.DataFrame(results)


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 60)
    print("台股活躍股預測程式 v2.1（證交所官方 API，僅上市股票）")
    print("=" * 60)

    print(f"\n[1/4] 抓取近 {LOOKBACK_DAYS} 個交易日全市場（上市）成交資料...")
    panel = fetch_panel_data(LOOKBACK_DAYS)
    print(f"  共取得 {len(panel):,} 筆資料，涵蓋日期：{sorted(panel['date'].unique())}")

    print("\n[2/4] 計算活躍度分數...")
    scored = compute_daily_scores(panel)

    print("\n[3/4] 計算趨勢並預測明日活躍度...")
    predicted = predict_next_day_activity(scored)
    predicted = predicted.sort_values("predicted_next_score", ascending=False)

    top10 = predicted.head(TOP_N).reset_index(drop=True)
    top10.insert(0, "rank", range(1, len(top10) + 1))

    print(f"\n[4/4] 預測明日活躍度前 {TOP_N} 名：")
    print("-" * 60)
    for _, row in top10.iterrows():
        print(
            f"  {int(row['rank']):>2}. {row['stock_id']} {row['stock_name']}"
            f"　預測分數={row['predicted_next_score']:.2f}"
            f"　最新分數={row['latest_score']:.2f}"
            f"　趨勢斜率={row['trend_slope']:+.2f}"
        )

    display_cols = [
        "rank",
        "stock_id",
        "stock_name",
        "predicted_next_score",
        "latest_score",
        "trend_slope",
        "latest_trading_money",
        "latest_volume",
        "latest_volume_ratio",
        "latest_amplitude",
        "latest_date",
    ]
    full_display_cols = [c for c in display_cols if c != "rank"]

    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        top10[display_cols].to_excel(writer, sheet_name="活躍度前10名", index=False)
        predicted[full_display_cols].to_excel(writer, sheet_name="全市場排名(僅上市)", index=False)

        params_df = pd.DataFrame(
            {
                "參數": [
                    "分析日期",
                    "資料來源",
                    "涵蓋範圍",
                    "回看交易日數",
                    "成交值權重",
                    "成交量變化率權重",
                    "成交筆數權重(換手率替代指標)",
                    "振幅權重",
                    "實際使用交易日",
                ],
                "數值": [
                    datetime.date.today().isoformat(),
                    "證交所官方公開API (twse.com.tw)",
                    "僅上市(TWSE)，不含上櫃/興櫃",
                    LOOKBACK_DAYS,
                    WEIGHT_TRADING_MONEY,
                    WEIGHT_VOLUME_RATIO,
                    WEIGHT_TURNOVER_COUNT,
                    WEIGHT_AMPLITUDE,
                    ", ".join(sorted(panel["date"].unique())),
                ],
            }
        )
        params_df.to_excel(writer, sheet_name="參數設定", index=False)

    print(f"\n完成！結果已輸出至：{OUTPUT_FILE}")
    print("\n免責聲明：本程式僅供技術參考，動能外推非機器學習預測，")
    print("不構成任何投資建議，請自行判斷並承擔交易風險。")


if __name__ == "__main__":
    main()
