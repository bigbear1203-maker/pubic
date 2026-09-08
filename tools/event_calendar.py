# -*- coding: utf-8 -*-
"""
台股事件日曆 v1.0（最小版）
===========================
用途只有兩個：**標記**與**統計**。

    1. 標記：告訴你未來幾天裡哪幾天是「事件日」。
    2. 統計：拿你既有的紀錄檔，比對事件日與一般日的實際表現差多少。

它刻意**不做**這些事（現階段是故意的，不是還沒寫）：

    ✗ 不進模型、不加特徵          — 事件樣本太少，加進去只會過擬合
    ✗ 不改評分、不改買賣建議      — 先看有沒有差異，再談要不要用
    ✗ 不動 Excel 欄位             — 紀錄檔已經為了欄位版本痛過一次

理由見對話紀錄：已排程事件的日期是公開資訊，市場早就定價，對「方向」
幾乎沒有預測力；有預測力的是「波動大小」。所以事件日曆的正當用途是
風險管理（那天要不要開新倉、停損要不要放寬），不是預測漲跌。

事件來源全部是**純日曆推算，不需要網路**：

    ┌────────────────────────┬──────────────────────────────────────┐
    │ 期貨結算日             │ 到期月份第三個星期三（休市則順延）    │
    │ 期貨結算前一交易日     │ 結算前的調整壓力通常提前出現          │
    │ 月營收公布截止日       │ 每月 10 日（非交易日則順延）          │
    │ 月營收公布後首個交易日 │ 盤後公布的營收，隔天才會反應在價格上   │
    └────────────────────────┴──────────────────────────────────────┘

除權息需要個股資料（要連網），預設不啟用，用 --with-dividends 才會抓。

用法：

    python stock.py events                  未來 14 天的事件
    python stock.py events --days 30        未來 30 天
    python stock.py events --month 2026-10  指定月份的完整事件表
    python stock.py events --stats          事件日 vs 一般日的實際表現比較
    python stock.py events --with-dividends 2330 2454   （需連網）

假日清單不自己維護，而是在執行時從 claude_stock_analyzer_v3.7.py 讀出來，
確保整套系統只有一份交易日曆。那支程式讀不到時會明講，不會默默用錯的清單。
"""

from __future__ import annotations

import argparse
import ast
import calendar
import datetime as dt
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------------
# 交易日曆：唯一來源是分析程式裡的 TW_HOLIDAYS
# ----------------------------------------------------------------------
# 為什麼用 ast 解析原始碼而不是 import？因為 claude_stock_analyzer_v3.7.py
# 在 module 層就 import 了 yfinance / sklearn，光是為了拿一份假日清單就把
# 那些套件全部載進來，既慢又容易在缺套件的機器上整支掛掉。ast 只讀語法樹，
# 不執行任何程式碼。
#
# 也刻意不在這裡自己複製一份假日清單。複製出來的第二份清單一定會跟本尊
# 走鐘，而且走鐘的時候不會有任何錯誤訊息——它只會安靜地把統計算錯。

_ANALYZER_GLOB = "claude_stock_analyzer_v3.*.py"


def _find_analyzer() -> Path | None:
    cands = sorted(ROOT.glob(_ANALYZER_GLOB))
    return cands[-1] if cands else None


def _date_from_call(node) -> dt.date | None:
    """把語法樹裡的 datetime.date(2026, 1, 1) 還原成真正的 date。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name != "date" or len(node.args) != 3:
        return None
    try:
        y, m, d = (ast.literal_eval(a) for a in node.args)
        return dt.date(y, m, d)
    except (ValueError, TypeError, SyntaxError):
        return None


def load_trading_calendar(analyzer: Path | None = None):
    """
    回傳 (holidays:set[date], coverage_until:date|None, source_note:str)。

    讀不到時回傳空集合，並在 source_note 裡明講——呼叫端必須把這個提醒
    顯示出來，因為「假日清單是空的」會讓所有事件日期悄悄算錯。
    """
    path = analyzer or _find_analyzer()
    if path is None:
        return set(), None, f"找不到 {_ANALYZER_GLOB}，無法取得台股假日清單"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as e:
        return set(), None, f"讀取 {path.name} 失敗：{e}"

    holidays: set[dt.date] = set()
    coverage: dt.date | None = None
    found_holidays = False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "TW_HOLIDAYS" in names and isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
            found_holidays = True
            for elt in node.value.elts:
                d = _date_from_call(elt)
                if d is not None:
                    holidays.add(d)
        elif "TW_HOLIDAY_COVERAGE_UNTIL" in names:
            coverage = _date_from_call(node.value)

    if not found_holidays:
        return set(), coverage, f"{path.name} 裡找不到 TW_HOLIDAYS"
    return holidays, coverage, ""


class TradingCalendar:
    """只負責回答「這天有沒有開市」，以及往前往後找交易日。"""

    def __init__(self, holidays=None, coverage_until=None, note=""):
        if holidays is None:
            holidays, coverage_until, note = load_trading_calendar()
        self.holidays = set(holidays)
        self.coverage_until = coverage_until
        self.note = note

    def is_trading_day(self, d: dt.date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def next_trading_day(self, d: dt.date) -> dt.date:
        d += dt.timedelta(days=1)
        while not self.is_trading_day(d):
            d += dt.timedelta(days=1)
        return d

    def prev_trading_day(self, d: dt.date) -> dt.date:
        d -= dt.timedelta(days=1)
        while not self.is_trading_day(d):
            d -= dt.timedelta(days=1)
        return d

    def roll_forward(self, d: dt.date) -> dt.date:
        """d 當天若休市則往後順延到最近的交易日；有開市就是它自己。"""
        while not self.is_trading_day(d):
            d += dt.timedelta(days=1)
        return d

    def coverage_warning(self, d: dt.date) -> str:
        if self.coverage_until is not None and d > self.coverage_until:
            return (f"假日清單只維護到 {self.coverage_until}，{d} 已超出涵蓋範圍，"
                    "事件日期可能沒有把國定假日算進去")
        return ""


# ----------------------------------------------------------------------
# 事件推算
# ----------------------------------------------------------------------

FUTURES_SETTLEMENT = "期貨結算日"
FUTURES_PRE = "期貨結算前一交易日"
REVENUE_DEADLINE = "月營收公布截止日"
REVENUE_NEXT = "月營收公布後首個交易日"
EX_DIVIDEND = "除權息"

# 每個事件對「該做什麼」的一句話說明。刻意不寫成「會漲」「會跌」——
# 事件能告訴你的是波動變大，不是方向。
EVENT_NOTES = {
    FUTURES_SETTLEMENT: "結算日現貨易受期貨部位調整影響，日內波動放大",
    FUTURES_PRE: "結算前調整壓力通常提前一天出現",
    REVENUE_DEADLINE: "上市櫃公司須於 10 日前公布上月營收，個股易有跳空",
    REVENUE_NEXT: "前一日盤後公布的營收，今天才會反應在價格上",
    EX_DIVIDEND: "價格有除權息斷點，報酬率計算需還原，不是真的下跌",
}

# 事件日的建議處置。只有兩件事：不開新倉、放寬停損。
# 沒有「加碼」「減碼」這種帶方向的建議，因為我們沒有方向上的證據。
EVENT_ACTIONS = {
    FUTURES_SETTLEMENT: "不開新倉；既有部位停損倍數放寬",
    FUTURES_PRE: "留意波動放大，新倉可延後一天",
    REVENUE_DEADLINE: "個股跳空風險高，不開新倉",
    REVENUE_NEXT: "開盤跳空可能已反應完營收，追價須謹慎",
    EX_DIVIDEND: "報酬率請用還原權值計算，否則會誤判為停損",
}

REVENUE_DEADLINE_DAY = 10  # 證交法規定每月 10 日前公布上月營收


def futures_settlement(year: int, month: int, cal: TradingCalendar) -> dt.date:
    """
    台指期結算日 = 到期月份第三個星期三；該日休市則順延至次一營業日。
    （台灣期交所規則。順延而非提前，這點常被寫反。）
    """
    first = dt.date(year, month, 1)
    offset = (calendar.WEDNESDAY - first.weekday()) % 7
    third_wed = first + dt.timedelta(days=offset + 14)
    return cal.roll_forward(third_wed)


def revenue_deadline(year: int, month: int, cal: TradingCalendar) -> dt.date:
    """
    月營收公布截止日 = 每月 10 日；非交易日則順延至次一交易日。
    公布的是「上個月」的營收。
    """
    return cal.roll_forward(dt.date(year, month, REVENUE_DEADLINE_DAY))


def month_events(year: int, month: int, cal: TradingCalendar) -> dict[dt.date, list[str]]:
    """回傳該月份（含溢出到鄰月的衍生日）的 {日期: [事件名稱, ...]}。"""
    events: dict[dt.date, list[str]] = {}

    def add(d: dt.date, name: str) -> None:
        events.setdefault(d, [])
        if name not in events[d]:
            events[d].append(name)

    settle = futures_settlement(year, month, cal)
    add(settle, FUTURES_SETTLEMENT)
    add(cal.prev_trading_day(settle), FUTURES_PRE)

    rev = revenue_deadline(year, month, cal)
    add(rev, REVENUE_DEADLINE)
    add(cal.next_trading_day(rev), REVENUE_NEXT)

    return events


def events_between(start: dt.date, end: dt.date,
                   cal: TradingCalendar) -> dict[dt.date, list[str]]:
    """
    取得 [start, end] 區間內的所有事件。

    會多算前後各一個月，因為「結算前一交易日」與「營收後首個交易日」
    可能落在鄰月——只算區間內的月份會漏掉跨月的那幾天。
    """
    months: set[tuple[int, int]] = set()
    d = dt.date(start.year, start.month, 1) - dt.timedelta(days=1)
    limit = end + dt.timedelta(days=40)
    while d <= limit:
        months.add((d.year, d.month))
        # 跳到下個月 1 日
        d = dt.date(d.year + (d.month == 12), (d.month % 12) + 1, 1)

    merged: dict[dt.date, list[str]] = {}
    for y, m in sorted(months):
        for day, names in month_events(y, m, cal).items():
            if start <= day <= end:
                merged.setdefault(day, [])
                for n in names:
                    if n not in merged[day]:
                        merged[day].append(n)
    return merged


def events_on(day: dt.date, cal: TradingCalendar) -> list[str]:
    """單日查詢。"""
    return events_between(day, day, cal).get(day, [])


# ----------------------------------------------------------------------
# 除權息（唯一需要網路的部分，預設不啟用）
# ----------------------------------------------------------------------

def ex_dividend_dates(symbols, start: dt.date, end: dt.date) -> dict[str, list[dt.date]]:
    """
    用 yfinance 查個股除權息日。抓不到就回傳空清單並印出原因，
    不讓它變成整個指令的致命錯誤——這是附加資訊，不是核心功能。
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  ⚠ 未安裝 yfinance，跳過除權息查詢")
        return {}

    out: dict[str, list[dt.date]] = {}
    for sym in symbols:
        ticker = sym if "." in sym else f"{sym}.TW"
        try:
            divs = yf.Ticker(ticker).dividends
        except Exception as e:                                  # noqa: BLE001
            print(f"  ⚠ {ticker} 除權息查詢失敗：{type(e).__name__}: {e}")
            continue
        if divs is None or len(divs) == 0:
            out[sym] = []
            continue
        days = []
        for ts in divs.index:
            d = ts.date() if hasattr(ts, "date") else ts
            if start <= d <= end:
                days.append(d)
        out[sym] = sorted(days)
    return out


# ----------------------------------------------------------------------
# 顯示
# ----------------------------------------------------------------------

WEEKDAY_ZH = "一二三四五六日"


def _dw(text: str) -> int:
    """中文字在終端機佔兩個字元寬。用字元數對齊表格會歪掉，要用顯示寬度。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def _pad(text: str, width: int, align: str = "<") -> str:
    """依顯示寬度補空白。align: '<' 靠左、'>' 靠右。"""
    text = str(text)
    gap = max(0, width - _dw(text))
    return (text + " " * gap) if align == "<" else (" " * gap + text)


def _fmt_day(d: dt.date, cal: TradingCalendar) -> str:
    mark = "" if cal.is_trading_day(d) else "（休市）"
    return f"{d} (週{WEEKDAY_ZH[d.weekday()]}){mark}"


def print_events(events: dict[dt.date, list[str]], cal: TradingCalendar,
                 today: dt.date | None = None) -> None:
    if not events:
        print("  這段期間沒有排程事件。")
        return
    today = today or dt.date.today()
    for d in sorted(events):
        rel = (d - today).days
        when = "今天" if rel == 0 else (f"{rel} 天後" if rel > 0 else f"{-rel} 天前")
        print(f"\n  {_fmt_day(d, cal)}   {when}")
        for name in events[d]:
            print(f"      • {name}")
            print(f"        {EVENT_NOTES.get(name, '')}")
            print(f"        建議：{EVENT_ACTIONS.get(name, '')}")
    warn = cal.coverage_warning(max(events))
    if warn:
        print(f"\n  ⚠ {warn}")


# ----------------------------------------------------------------------
# 統計：事件日 vs 一般日
# ----------------------------------------------------------------------
# 這是整個工具真正的目的。先看有沒有差異，有差異再談要不要用。
#
# 比較的是「預測目標日」而不是「執行日」：實際報酬是目標日那天實現的，
# 用執行日去對事件會整個錯開一天。

TARGET_DATE_COL = "預測目標日(隔日估計)"
RETURN_COL = "實際報酬(%)"
HIT_COLS = {"綜合分數": "是否命中_綜合分數", "邏輯迴歸": "是否命中_邏輯迴歸",
            "RF": "是否命中_RF"}

# 樣本數低於這個數字時，任何差異都不該當成證據。
# 跟 wilson_lower_bound 是同一個立場：小樣本上的漂亮數字沒有意義。
MIN_MEANINGFUL_N = 30


def _to_date(v):
    """把紀錄檔裡的日期欄位轉成 date；轉不動回傳 None。"""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s[:len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _hit_rate(series):
    """是否命中欄位可能是 True/False、'是'/'否'、1/0，全部收斂成比例。"""
    vals = []
    for v in series:
        if isinstance(v, bool):
            vals.append(v)
        elif isinstance(v, (int, float)) and v in (0, 1):
            vals.append(bool(v))
        else:
            s = str(v).strip().lower()
            if s in ("true", "是", "1", "y", "yes"):
                vals.append(True)
            elif s in ("false", "否", "0", "n", "no"):
                vals.append(False)
    if not vals:
        return None, 0
    return sum(vals) / len(vals) * 100, len(vals)


def _describe(df, label: str) -> dict:
    import numpy as np
    r = df[RETURN_COL].astype(float)
    out = {"label": label, "n": len(r),
           "mean": float(r.mean()) if len(r) else float("nan"),
           "abs_mean": float(r.abs().mean()) if len(r) else float("nan"),
           "std": float(r.std(ddof=1)) if len(r) > 1 else float("nan"),
           "worst": float(r.min()) if len(r) else float("nan"),
           "best": float(r.max()) if len(r) else float("nan")}
    for name, col in HIT_COLS.items():
        if col in df.columns:
            rate, n = _hit_rate(df[col])
            out[f"hit_{name}"] = rate
            out[f"hitn_{name}"] = n
    _ = np
    return out


def event_stats(log_path: Path, cal: TradingCalendar) -> int:
    """比較事件日與一般日的已實現報酬。回傳 exit code。"""
    try:
        import pandas as pd
    except ImportError:
        print("✗ 需要 pandas 才能讀紀錄檔：pip install pandas openpyxl")
        return 1

    print(f"\n讀取紀錄檔：{log_path.name}")
    try:
        df = pd.read_excel(log_path)
    except Exception as e:                                      # noqa: BLE001
        print(f"✗ 讀取失敗：{type(e).__name__}: {e}")
        return 1

    for col in (TARGET_DATE_COL, RETURN_COL):
        if col not in df.columns:
            print(f"✗ 紀錄檔缺少「{col}」欄位。")
            print("  先跑 python stock.py review 回填實際結果，才會有東西可以比。")
            return 1

    total = len(df)
    df = df[df[RETURN_COL].notna()].copy()
    if df.empty:
        print(f"✗ {total} 筆紀錄裡沒有任何一筆回填了實際報酬。")
        print("  先跑 python stock.py review。")
        return 1

    df["_target"] = df[TARGET_DATE_COL].map(_to_date)
    unparsed = int(df["_target"].isna().sum())
    df = df[df["_target"].notna()].copy()
    if df.empty:
        print("✗ 沒有任何一筆的預測目標日可以解析。")
        return 1

    lo, hi = df["_target"].min(), df["_target"].max()
    ev = events_between(lo, hi, cal)
    df["_events"] = df["_target"].map(lambda d: ev.get(d, []))
    df["_is_event"] = df["_events"].map(bool)

    print(f"  已回填實際結果 {len(df)} 筆 / 全部 {total} 筆")
    if unparsed:
        print(f"  ⚠ 有 {unparsed} 筆的預測目標日無法解析，已排除")
    print(f"  涵蓋期間 {lo} ~ {hi}")
    warn = cal.coverage_warning(hi)
    if warn:
        print(f"  ⚠ {warn}")

    ev_df = df[df["_is_event"]]
    normal_df = df[~df["_is_event"]]
    rows = [_describe(ev_df, "事件日"), _describe(normal_df, "一般日")]

    print("\n" + "=" * 68)
    print("  事件日 vs 一般日（已實現報酬）")
    print("=" * 68)
    heads = ["", "筆數", "平均絕對報酬", "標準差", "平均報酬", "最差", "最好"]
    widths = [10, 7, 14, 10, 11, 10, 10]
    print("\n  " + "".join(_pad(h, w, ">" if i else "<")
                           for i, (h, w) in enumerate(zip(heads, widths))))
    print("  " + "-" * sum(widths))
    for r in rows:
        cells = [_pad(r["label"], widths[0])]
        if r["n"] == 0:
            cells += [_pad(r["n"], widths[1], ">"), _pad("（無資料）", widths[2], ">")]
        else:
            for val, w, suf in ((r["n"], widths[1], ""),
                                (r["abs_mean"], widths[2], "%"),
                                (r["std"], widths[3], ""),
                                (r["mean"], widths[4], "%"),
                                (r["worst"], widths[5], "%"),
                                (r["best"], widths[6], "%")):
                txt = f"{val:d}" if isinstance(val, int) else f"{val:.2f}{suf}"
                cells.append(_pad(txt, w, ">"))
        print("  " + "".join(cells))

    # 命中率
    have_hits = [n for n in HIT_COLS if any(f"hit_{n}" in r for r in rows)]
    if have_hits:
        hw = 12
        print("\n  " + _pad("命中率", 10)
              + "".join(_pad(n, hw, ">") for n in have_hits))
        print("  " + "-" * (10 + hw * len(have_hits)))
        for r in rows:
            cells = ""
            for n in have_hits:
                v = r.get(f"hit_{n}")
                cells += _pad(f"{v:.1f}%" if v is not None else "（無）", hw, ">")
            print("  " + _pad(r["label"], 10) + cells)

    # 分事件類型
    print("\n  分事件類型（同一天可能同時屬於多個事件）")
    print("  " + "-" * 60)
    any_type = False
    for name in (FUTURES_SETTLEMENT, FUTURES_PRE, REVENUE_DEADLINE, REVENUE_NEXT):
        sub = df[df["_events"].map(lambda names, n=name: n in names)]
        if sub.empty:
            continue
        any_type = True
        d = _describe(sub, name)
        absmean = f"{d['abs_mean']:.2f}%"
        print(f"  {_pad(name, 24)}{_pad(d['n'], 5, '>')} 筆"
              f"   平均絕對報酬 {_pad(absmean, 7, '>')}"
              f"   平均 {d['mean']:+.2f}%")
    if not any_type:
        print("  （這段期間內沒有任何一筆落在事件日）")

    # 結論 —— 樣本數不夠就直說，不要給出看起來很像結論的東西
    print("\n" + "=" * 68)
    ev_n, norm_n = rows[0]["n"], rows[1]["n"]
    if ev_n < MIN_MEANINGFUL_N or norm_n < MIN_MEANINGFUL_N:
        print(f"  ⚠ 樣本數不足（事件日 {ev_n} 筆、一般日 {norm_n} 筆，"
              f"兩邊都需要至少 {MIN_MEANINGFUL_N} 筆）。")
        print("    現在看到的任何差異都還在雜訊範圍內，不要當成證據。")
        print("    繼續每天跑 python stock.py daily 累積，一個月後再看一次。")
    else:
        diff = rows[0]["abs_mean"] - rows[1]["abs_mean"]
        pct = diff / rows[1]["abs_mean"] * 100 if rows[1]["abs_mean"] else 0.0
        print(f"  事件日的平均絕對報酬比一般日{'高' if diff >= 0 else '低'} "
              f"{abs(diff):.2f} 個百分點（{pct:+.0f}%）。")
        print("  這是波動差異，不是方向差異——不代表事件日該買或該賣。")
        print("  若事件日波動明顯較大，合理的做法是那天不開新倉、或放寬停損倍數，")
        print("  而不是把事件放進模型當特徵（事件樣本太少，加了會過擬合）。")
    print("=" * 68 + "\n")
    return 0


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _find_log() -> Path | None:
    exclude = ("備份", "old file", "fallback", "已修復", "修復前", "加建議前")
    cands = [p for p in ROOT.glob("stock_analysis_log*.xlsx")
             if not any(k in p.name for k in exclude)]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="台股事件日曆：標記事件日，並比較事件日與一般日的實際表現")
    ap.add_argument("--days", type=int, default=14,
                    help="往後看幾天（預設 14）")
    ap.add_argument("--month", metavar="YYYY-MM",
                    help="改列出指定月份的完整事件表")
    ap.add_argument("--stats", action="store_true",
                    help="用既有紀錄檔比較事件日 vs 一般日")
    ap.add_argument("--log", metavar="PATH", help="指定紀錄檔（預設自動尋找）")
    ap.add_argument("--with-dividends", nargs="*", metavar="代碼",
                    help="額外查詢這些個股的除權息日（需連網）")
    args = ap.parse_args(argv)

    cal = TradingCalendar()
    if cal.note:
        print(f"⚠ {cal.note}")
        print("  沒有假日清單時，遇到國定假日的事件日期會算錯。請先確認"
              f"{_ANALYZER_GLOB} 在同一個資料夾裡。\n")

    if args.stats:
        log = Path(args.log) if args.log else _find_log()
        if log is None or not log.exists():
            print("✗ 找不到紀錄檔 stock_analysis_log*.xlsx")
            return 1
        return event_stats(log, cal)

    today = dt.date.today()
    if args.month:
        try:
            y, m = (int(x) for x in args.month.split("-"))
            start = dt.date(y, m, 1)
            end = dt.date(y, m, calendar.monthrange(y, m)[1])
        except (ValueError, TypeError):
            print(f"✗ 月份格式應為 YYYY-MM，收到「{args.month}」")
            return 1
        title = f"{y} 年 {m} 月事件表"
    else:
        start, end = today, today + dt.timedelta(days=args.days)
        title = f"未來 {args.days} 天的事件（{start} ~ {end}）"

    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)
    print_events(events_between(start, end, cal), cal, today)

    if args.with_dividends is not None:
        syms = args.with_dividends or []
        if not syms:
            print("\n  --with-dividends 後面要接股票代碼，例如 --with-dividends 2330 2454")
        else:
            print(f"\n  除權息查詢（{start} ~ {end}）")
            found = ex_dividend_dates(syms, start, end)
            for sym in syms:
                days = found.get(sym)
                if days is None:
                    continue
                if days:
                    for d in days:
                        print(f"    {sym}  {_fmt_day(d, cal)}  {EX_DIVIDEND}")
                        print(f"          {EVENT_ACTIONS[EX_DIVIDEND]}")
                else:
                    print(f"    {sym}  這段期間沒有除權息")

    print("\n  這些事件只用來決定「那天要不要開新倉」，不用來判斷漲跌方向。")
    print("  累積一個月後跑 python stock.py events --stats 看有沒有實際差異。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
