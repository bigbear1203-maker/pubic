# -*- coding: utf-8 -*-
"""
claude_stock_analyzer 資料品質防護模組 v1.0
============================================
這是給 claude_stock_analyzer v3.6 用的「外掛式」補強，不改動它既有的
分析邏輯，只在幾個關鍵位置加上把關，擋掉會讓輸出失真的情況。

依據：2026-08-26 ~ 08-28 累積的 80 筆 stock_analysis_log 實際紀錄診斷
（詳見 docs/程式診斷報告.md）。

套用方式：把本檔放到與 claude_stock_analyzer_v3.6.py 同目錄，然後在
analyzer 裡 `import data_quality as dq`，照各函式 docstring 的說明插入。
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

# ============================================================
# 1. 未完成 K 棒偵測（最高優先）
# ============================================================

# 台股連續交易時段
TW_SESSION_OPEN = dt.time(9, 0)
TW_SESSION_CLOSE = dt.time(13, 30)
# 收盤後緩衝：資料源更新需要時間，太早抓仍可能拿到未定案的收盤價
TW_DATA_SETTLE = dt.time(14, 0)

US_SESSION_OPEN = dt.time(9, 30)
US_SESSION_CLOSE = dt.time(16, 0)


def is_intraday_now(symbol: str, now: dt.datetime | None = None) -> bool:
    """
    現在是否處於該市場的盤中時段。台股用本機時間判斷（假設你在台灣執行）；
    美股因為時區換算牽涉夏令時間，這裡不猜，一律回傳 False，改由
    trim_incomplete_bar() 用「最後一根 K 棒的日期是不是今天」來判斷。
    """
    now = now or dt.datetime.now()
    if not str(symbol).upper().endswith((".TW", ".TWO")):
        return False
    return TW_SESSION_OPEN <= now.time() < TW_DATA_SETTLE


def trim_incomplete_bar(hist: pd.DataFrame, symbol: str,
                        now: dt.datetime | None = None) -> tuple[pd.DataFrame, bool]:
    """
    ⚠ 這是本模組最重要的一個函式。

    問題：yfinance 的 history() 在盤中會把「今天這根還沒收盤的 K 棒」一起
    回傳，此時 Close 其實是當下成交價，不是收盤價。而模型的
    intraday_return = (Close - Open) / Open 這個特徵，訓練時看到的全是
    完整交易日的開盤到收盤，盤中拿到的卻是「開盤到現在」——分布不同，
    模型等於在沒看過的輸入上做外插。

    實測證據：2891C.TW 在 2026-08-28 13:15（盤中）算出上漲機率 73.9%，
    同一天 15:01（收盤後）用同一個「資料基準日」算出 34.8%，
    差距 39.2 個百分點，而且方向完全相反。整份紀錄唯一一次發出的
    Sell 訊號，就是這樣來的。

    處理：盤中執行時，把最後一根未完成 K 棒剔除，改用前一個完整交易日
    當基準。這會讓盤中執行的結果等同於「昨天收盤後跑的結果」——這是
    誠實的做法：盤中本來就還沒有今天的收盤資訊。

    回傳 (處理後的 hist, 是否有剔除)。

    【插入位置】claude_stock_analyzer_v3.6.py 的 analyze() 內，
        hist = ticker_obj.history(period="3y", auto_adjust=True)
        if hist.empty: ...
    這兩行之後、所有 calc_* 之前，加上：
        hist, trimmed = dq.trim_incomplete_bar(hist, ticker_symbol)
        if trimmed:
            print("⚠ 盤中執行：已剔除今日未完成K棒，以前一交易日收盤為基準")
    """
    if hist is None or hist.empty:
        return hist, False

    last_date = hist.index[-1].date()
    now = now or dt.datetime.now()

    # 條件：最後一根 K 棒就是今天，而且現在還在盤中/剛收盤未定案
    if last_date == now.date() and is_intraday_now(symbol, now):
        return hist.iloc[:-1].copy(), True
    return hist, False


# ============================================================
# 2. 資料新鮮度
# ============================================================

def check_price_freshness(hist: pd.DataFrame, max_lag_days: int = 4,
                          now: dt.datetime | None = None) -> dict:
    """
    價格資料是否停滯。

    實測證據：00684R.TW 在 2026-08-28 跑了三次（08:10 / 13:24 / 15:10），
    「資料基準日」全都停在 2026-08-26、股價全都是 15.25 沒動過，
    但程式照樣輸出了上漲機率 31.0% 與綜合結論「中性偏多」。
    抓不到新資料時應該要說「抓不到」，而不是拿兩天前的資料當今天用。

    max_lag_days=4 是為了涵蓋週末（週五收盤 → 週二執行 = 4 天）。

    【插入位置】analyze() 內取得 price_date 之後：
        fresh = dq.check_price_freshness(hist)
        if not fresh["ok"]:
            print(f"⚠ {fresh['message']}")
            # 建議：把 fresh["stale"] 一併寫進 Excel 紀錄，方便事後過濾
    """
    now = now or dt.datetime.now()
    if hist is None or hist.empty:
        return {"ok": False, "stale": True, "lag_days": None,
                "message": "沒有任何價格資料"}

    last_date = hist.index[-1].date()
    lag = (now.date() - last_date).days
    stale = lag > max_lag_days
    return {
        "ok": not stale,
        "stale": stale,
        "lag_days": lag,
        "last_date": last_date,
        "message": (
            f"價格資料停滯：最新交易日 {last_date}，距今 {lag} 天，"
            f"超過容許的 {max_lag_days} 天。本次結果不應採用。"
            if stale else f"價格資料為 {last_date}（距今 {lag} 天）"
        ),
    }


# ============================================================
# 3. 小樣本準確率保護
# ============================================================

def assess_accuracy_reliability(accuracy_pct: float | None, sample_size: int | None,
                                baseline_pct: float = 50.0) -> dict:
    """
    走勢模型回報的「樣本外準確率」在樣本數少的時候完全不能信。

    實測證據：2891C.TW 回報樣本外準確率 74~77%，是全部 19 檔裡最高的，
    也是整份紀錄唯一觸發 Sell 訊號的一檔；其餘標的的準確率中位數只有
    51.4%（邏輯迴歸）／52.4%（RF）。這個 74% 到底是真優勢還是小樣本
    造成的假象，目前「無法判斷」——因為 walk-forward 明明算出了
    sample_size，EXCEL_LOG_COLUMNS 卻沒有把它記下來。一個會被拿來
    發出交易訊號的數字，卻沒有記錄它是幾筆樣本算出來的，這本身就是
    要優先修掉的缺口。

    這裡用 Wilson 信賴區間下限來判斷：只有「連信賴區間下限都高於基準」
    才算真的有優勢。實際門檻隨樣本數變動：
        n=  60 → 準確率需 ≥62.7%
        n= 250 → 準確率需 ≥56.2%
        n= 450 → 準確率需 ≥54.6%
        n=1000 → 準確率需 ≥53.1%
    樣本越少門檻越嚴，這正是小樣本該有的待遇。

    ⚠ 另外請注意：analyzer 的 walk-forward 有算出 sample_size，
    但 EXCEL_LOG_COLUMNS 沒有把它記下來，所以事後無法判斷任何一筆
    準確率是幾筆樣本算出來的。建議在 Excel 紀錄新增樣本數欄位。
    """
    if accuracy_pct is None or sample_size is None or sample_size <= 0:
        return {"reliable": False, "label": "unknown", "lower_bound": None,
                "message": "缺少準確率或樣本數，無法判斷可靠性"}

    p = accuracy_pct / 100.0
    n = int(sample_size)
    z = 1.96
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    lower = (centre - half) * 100

    reliable = lower > baseline_pct
    if reliable:
        label = "usable_with_caution"
    elif accuracy_pct >= 55:
        label = "weak"          # 點估計看起來不錯，但信賴區間蓋到 50%
    else:
        label = "not_reliable"

    return {
        "reliable": reliable,
        "label": label,
        "lower_bound": lower,
        "sample_size": n,
        "message": (
            f"準確率 {accuracy_pct:.1f}%（n={n}），95% 信賴區間下限 {lower:.1f}%，"
            + ("高於 50%，有統計上的優勢跡象。" if reliable
               else f"未高於 {baseline_pct:.0f}%，無法排除只是隨機波動。")
        ),
    }


# ============================================================
# 4. 交易日曆（含台股國定假日）
# ============================================================

# 只列出已確認的休市日。清單過期時 next_trading_day() 會明確告訴你，
# 而不是默默算出錯的日期。維護來源：TWSE 每年公告的「有價證券集中交易
# 市場開（休）市日期表」。
TW_HOLIDAYS: set[dt.date] = {
    # 2026 年（請每年更新；未列出的日期一律視為有開市）
    dt.date(2026, 1, 1),                      # 元旦
    dt.date(2026, 2, 14), dt.date(2026, 2, 16), dt.date(2026, 2, 17),
    dt.date(2026, 2, 18), dt.date(2026, 2, 19), dt.date(2026, 2, 20),  # 春節
    dt.date(2026, 2, 27), dt.date(2026, 2, 28),  # 和平紀念日
    dt.date(2026, 4, 3), dt.date(2026, 4, 6),    # 清明/兒童節
    dt.date(2026, 5, 1),                      # 勞動節
    dt.date(2026, 6, 19),                     # 端午節
    dt.date(2026, 9, 25),                     # 中秋節
    dt.date(2026, 10, 9), dt.date(2026, 10, 26),  # 國慶/光復節
}

TW_HOLIDAY_COVERAGE_UNTIL = dt.date(2026, 12, 31)


def next_trading_day(basis_date: dt.date,
                     holidays: set[dt.date] | None = None) -> tuple[dt.date, str]:
    """
    推算下一個交易日，會跳過週末「與」國定假日。

    analyzer v3.6 的 next_trading_day_estimate() 只跳週末，遇到連假就會
    把「預測目標日」標錯——那會直接讓事後的命中率統計對錯日子，
    是會靜靜汙染整份驗證結果的那種錯。

    回傳 (日期, 提醒訊息)。假日清單過期時會在訊息裡明講。
    """
    holidays = TW_HOLIDAYS if holidays is None else holidays
    d = basis_date + dt.timedelta(days=1)
    while d.weekday() >= 5 or d in holidays:
        d += dt.timedelta(days=1)

    note = ""
    if d > TW_HOLIDAY_COVERAGE_UNTIL:
        note = (f"⚠ 假日清單只維護到 {TW_HOLIDAY_COVERAGE_UNTIL}，"
                f"{d} 已超出範圍，請自行核對是否為交易日")
    return d, note


# ============================================================
# 5. 以「期望值 > 交易成本」取代固定信心門檻
# ============================================================

# 台股來回成本：手續費 0.1425% × 折扣 × 2 + 賣出證交稅 0.3%
def round_trip_cost_pct(fee_discount: float = 0.6, day_trade: bool = False) -> float:
    tax = 0.15 if day_trade else 0.3
    return 0.1425 * fee_discount * 2 + tax


def expectancy_gate(p_up: float | None, avg_gain_pct: float | None,
                    avg_loss_pct: float | None, reliability: dict,
                    fee_discount: float = 0.6,
                    min_edge_pct: float = 0.1) -> tuple[str, dict]:
    """
    決策層改良版：不再問「兩個模型是不是都超過 58%」，而是問
    「照這個機率下注，扣掉手續費和證交稅之後，期望值還是正的嗎？」

    為什麼要改：v3.6 的規則是「雙模型方向一致 + 雙方信心都 ≥58% +
    準確率 ≥55%」。實測 80 筆紀錄裡，兩模型方向一致率只有 57.1%
    （機率相關係數 0.47），而準確率中位數只有 51~52%，三個條件同時
    成立的機率極低——結果就是 80 筆裡 79 筆是 Wait，唯一那筆 Sell
    還來自盤中未完成 K 棒造成的假訊號。一個永遠不出手的系統，
    對投資決策沒有任何幫助；問題不在門檻設太高，在於它擋掉的是
    「模型沒有優勢」這件事，而那件事應該用期望值講清楚，不是用
    一個拍腦袋的 58% 去擋。

    EV = p_up × 平均獲利 − (1 − p_up) × 平均虧損 − 來回成本

    回傳 (decision, detail)。decision ∈ {"Buy", "Sell", "Wait"}。
    """
    cost = round_trip_cost_pct(fee_discount)
    detail = {"cost_pct": cost, "ev_pct": None, "reason": ""}

    if p_up is None or avg_gain_pct is None or avg_loss_pct is None:
        detail["reason"] = "缺少機率或損益統計，無法計算期望值"
        return "Wait", detail

    if not reliability.get("reliable", False):
        detail["reason"] = (
            "模型準確率的信賴區間下限未高於 50%，優勢無法與隨機區分 → "
            + reliability.get("message", "")
        )
        return "Wait", detail

    p = float(p_up)
    ev_long = p * avg_gain_pct - (1 - p) * avg_loss_pct - cost
    ev_short = (1 - p) * avg_gain_pct - p * avg_loss_pct - cost
    detail["ev_long_pct"] = ev_long
    detail["ev_short_pct"] = ev_short

    if ev_long >= ev_short and ev_long > min_edge_pct:
        detail["ev_pct"] = ev_long
        detail["reason"] = f"做多期望值 {ev_long:+.2f}%（已扣 {cost:.2f}% 來回成本）"
        return "Buy", detail
    if ev_short > ev_long and ev_short > min_edge_pct:
        detail["ev_pct"] = ev_short
        detail["reason"] = f"做空期望值 {ev_short:+.2f}%（已扣 {cost:.2f}% 來回成本）"
        return "Sell", detail

    detail["ev_pct"] = max(ev_long, ev_short)
    detail["reason"] = (
        f"最佳方向期望值僅 {max(ev_long, ev_short):+.2f}%，"
        f"未超過 {min_edge_pct:.2f}% 的最低邊際要求（來回成本 {cost:.2f}%）"
    )
    return "Wait", detail


# ============================================================
# 6. 分析前的整體把關
# ============================================================

def should_analyze(hist: pd.DataFrame, symbol: str, min_bars: int = 300) -> dict:
    """
    在跑完整分析之前，先判斷這檔標的的資料夠不夠格被分析。

    實測證據：020020.TW（債券 ETF）連跑三次，市場狀態 unknown、
    ADX / 兩個模型機率全部是 NaN，卻仍然寫入紀錄、綜合分數給 0、
    結論寫「中性（訊號混合）」——這會讓人以為系統看過它了，
    實際上系統根本沒有資料可看。「資料不足」跟「分析後認為中性」
    是兩件完全不同的事，不該長得一樣。

    回傳 {"ok": bool, "reason": str}。ok=False 時建議直接跳過該檔，
    或在 Excel 紀錄把 Strategy_Decision 記為 "Skipped_資料不足"。
    """
    if hist is None or hist.empty:
        return {"ok": False, "reason": "查無價格資料"}
    if len(hist) < min_bars:
        return {"ok": False,
                "reason": f"歷史資料僅 {len(hist)} 個交易日，少於模型所需的 {min_bars} 日"}
    if hist["Close"].tail(60).nunique() <= 1:
        return {"ok": False, "reason": "近 60 日收盤價無變動，可能是流動性極低或資料異常"}
    if hist["Volume"].tail(20).median() <= 0:
        return {"ok": False, "reason": "近 20 日成交量中位數為 0，流動性不足以交易"}
    return {"ok": True, "reason": ""}
