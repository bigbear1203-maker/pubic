# -*- coding: utf-8 -*-
"""
台股紙上交易模擬器 v1.0
========================
用 100 萬新台幣的虛擬資金，跑一週（或任意期間）的模擬投資，週五結算。
訊號來源是 claude_stock_analyzer_v3.7 寫出的 stock_analysis_log_v3.7.xlsx，
不重新實作任何分析邏輯。

⚠ 為什麼是「多策略平行」而不是單一策略
--------------------------------------
你現有系統的 Strategy_Decision 在 80 筆紀錄裡有 79 筆是 Wait，
model_quality 零筆合格。如果模擬器只在 Buy/Sell 時出手，下週五你會
看到「整週沒有任何交易、獲利 0」——那等於白做一週。

所以這裡同時跑 6 個**各自獨立、各自都有 100 萬**的虛擬帳戶。它們用
同一批分析結果、不同的選股規則，週五直接比較。這樣不論訊號有沒有
出現，你都會得到可比較的資訊：

    strategy_decision  只在 Strategy_Decision=Buy 時買進（你現行的規則）
    ev_decision        只在 EV_Decision=Buy 時買進（v3.7 的影子規則）
    score_topn         綜合分數 > 0 的前 N 名，等權買進
    prob_topn          隔日上漲機率最高的前 N 名，等權買進
    active_equal       當日活躍度前 N 名等權買進（測試「活躍≠會漲」）
    cash               全現金，什麼都不做

最後一個 cash 帳戶不是湊數的：**「什麼都不做」是最重要的對照組**。
任何策略如果贏不過它，那個策略就是在付手續費買刺激。

⚠ 一週的結果在統計上等於零
--------------------------
5 個交易日、十來檔股票，而且同一天所有股票會一起漲跌（大盤一動全動），
有效獨立樣本數大約是 **1**。這一週的結果**不能**用來判斷任何策略好壞。

那為什麼還要做？三個實際理由：
  1. 驗證整條流程跑得通（抓資料→分析→下單→結算）
  2. 逼出實務問題（例如 100 萬買不起一張台積電，這件事只有真的下單才會發現）
  3. 建立每天記錄的紀律，這才是六個月後真正有價值的東西

模擬邏輯（刻意保守，不美化）
----------------------------
  • 訊號在 D 日收盤後產生，D+1 日**開盤價**成交（不是用收盤價回頭買）
  • 買進成本 = 手續費 0.1425% × 折扣（最低 20 元）
  • 賣出成本 = 手續費 + 證券交易稅 0.3%
  • 支援零股：100 萬買不起一張 2330（一張約 242 萬），不支援零股就只能
    看著訊號乾瞪眼。預設允許零股，可用 --lot-only 改成只買整張
  • 停損：跌破進場價 -7% 或 -2×ATR（取較近者），於收盤價出場
  • 單一標的上限 25%，最多同時持有 N 檔
  • 期末（週五收盤）全部出清，計入賣出成本

使用方式
--------
    # 1. 初始化（只做一次）
    python tools/paper_trading.py init --capital 1000000

    # 2. 每個交易日收盤後執行一次（會自動抓當日開/收盤價）
    python tools/paper_trading.py step --date 2026-08-31 \\
        --log stock_analysis_log_v3.7.xlsx

    # 3. 最後一天結算
    python tools/paper_trading.py settle --date 2026-09-04 \\
        --log stock_analysis_log_v3.7.xlsx

    # 4. 看報告（隨時可看）
    python tools/paper_trading.py report -o 模擬結算報告.xlsx

本模擬器不下真單、不連接任何券商，純粹是紙上推演，不構成投資建議。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# 參數
# ============================================================

DEFAULT_STATE = Path("paper_trading_state.json")

FEE_RATE = 0.001425
FEE_DISCOUNT = 0.6      # 券商手續費折扣，請依你實際使用的券商調整
FEE_MIN = 20.0          # 單筆最低手續費
TAX_SELL = 0.003        # 賣出證券交易稅
SHARES_PER_LOT = 1000

STRATEGIES = ("strategy_decision", "ev_decision", "score_topn",
              "prob_topn", "active_equal", "cash")

# 排名型策略：規則是「持有當下的前 N 名」，所以跌出名單就換掉。
RANKING_STRATEGIES = ("score_topn", "prob_topn", "active_equal")
# 訊號型策略：規則是「看到 Buy 才進場」，Wait 代表沒有意見，不是叫你賣，
# 所以只在出現明確 Sell 或超過最長持有天數時才出場。
SIGNAL_STRATEGIES = ("strategy_decision", "ev_decision")

STRATEGY_DESC = {
    "strategy_decision": "只在 Strategy_Decision=Buy 時買進（現行規則）",
    "ev_decision": "只在 EV_Decision=Buy 時買進（v3.7 影子規則）",
    "score_topn": "綜合分數>0 的前 N 名等權買進",
    "prob_topn": "隔日上漲機率最高的前 N 名等權買進",
    "active_equal": "分析清單前 N 名等權買進（活躍度對照組）",
    "cash": "全現金，什麼都不做（最重要的對照組）",
}


# ============================================================
# 成本模型
# ============================================================

def buy_cost(amount: float, fee_discount: float = FEE_DISCOUNT) -> float:
    return max(amount * FEE_RATE * fee_discount, FEE_MIN)


def sell_cost(amount: float, fee_discount: float = FEE_DISCOUNT) -> float:
    return max(amount * FEE_RATE * fee_discount, FEE_MIN) + amount * TAX_SELL


def round_trip_cost_pct(fee_discount: float = FEE_DISCOUNT) -> float:
    return (FEE_RATE * fee_discount * 2 + TAX_SELL) * 100


# ============================================================
# 價格取得
# ============================================================

def fetch_prices(symbols, date: dt.date, offline: pd.DataFrame | None = None) -> dict:
    """
    取得指定日期每檔股票的開盤價與收盤價。
    回傳 {symbol: {"open": float, "close": float}}，抓不到的標的就不會出現在結果裡。

    offline: 測試用。DataFrame 需含 date / symbol / open / close 欄位。
    """
    if offline is not None:
        sub = offline[pd.to_datetime(offline["date"]).dt.date == date]
        return {
            r["symbol"]: {"open": float(r["open"]), "close": float(r["close"])}
            for _, r in sub.iterrows()
        }

    try:
        import yfinance as yf
    except ImportError:
        print("  ✗ 未安裝 yfinance，無法取得價格 (pip install yfinance)")
        return {}

    out = {}
    for sym in sorted(set(symbols)):
        try:
            hist = yf.Ticker(str(sym)).history(
                start=date - dt.timedelta(days=5),
                end=date + dt.timedelta(days=1),
                auto_adjust=True,
            )
            row = hist[hist.index.date == date]
            if row.empty:
                print(f"  ⚠ {sym}: {date} 無交易資料（可能停牌或非交易日）")
                continue
            out[sym] = {"open": float(row["Open"].iloc[0]),
                        "close": float(row["Close"].iloc[0])}
        except Exception as e:
            print(f"  ⚠ {sym}: 取價失敗 {type(e).__name__}: {e}")
    return out


# ============================================================
# 投資組合
# ============================================================

class Portfolio:
    """單一虛擬帳戶。所有金額單位為新台幣元。"""

    def __init__(self, name: str, cash: float, data: dict | None = None):
        self.name = name
        if data:
            self.cash = data["cash"]
            self.positions = data["positions"]
            self.trades = data["trades"]
            self.equity_curve = data["equity_curve"]
            self.pending = data.get("pending", [])
        else:
            self.cash = float(cash)
            self.positions = {}      # symbol -> {shares, avg_cost, entry_date, stop_price}
            self.trades = []
            self.equity_curve = []   # [{date, equity, cash, n_positions}]
            self.pending = []        # 下一個交易日開盤要執行的單

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {"cash": self.cash, "positions": self.positions, "trades": self.trades,
                "equity_curve": self.equity_curve, "pending": self.pending}

    # ---------- 估值 ----------
    def market_value(self, prices: dict, field: str = "close") -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            px = prices.get(sym, {}).get(field)
            if px is None:
                px = pos["avg_cost"]  # 取不到價就用成本價估，並在報告中標示
            total += pos["shares"] * px
        return total

    def equity(self, prices: dict, field: str = "close") -> float:
        return self.cash + self.market_value(prices, field)

    # ---------- 交易 ----------
    def execute_buy(self, symbol: str, shares: int, price: float, date: dt.date,
                    reason: str, stop_price: float | None = None,
                    fee_discount: float = FEE_DISCOUNT) -> bool:
        if shares <= 0 or price <= 0:
            return False
        amount = shares * price
        cost = buy_cost(amount, fee_discount)
        if amount + cost > self.cash + 1e-6:
            return False
        self.cash -= amount + cost
        pos = self.positions.get(symbol)
        if pos:
            total_shares = pos["shares"] + shares
            pos["avg_cost"] = (pos["avg_cost"] * pos["shares"] + amount) / total_shares
            pos["shares"] = total_shares
            if stop_price is not None:
                pos["stop_price"] = stop_price
        else:
            self.positions[symbol] = {
                "shares": shares, "avg_cost": price,
                "entry_date": str(date), "stop_price": stop_price,
            }
        self.trades.append({
            "date": str(date), "symbol": symbol, "side": "買進", "shares": shares,
            "price": price, "amount": amount, "cost": cost, "reason": reason,
        })
        return True

    def execute_sell(self, symbol: str, price: float, date: dt.date, reason: str,
                     shares: int | None = None,
                     fee_discount: float = FEE_DISCOUNT) -> bool:
        pos = self.positions.get(symbol)
        if not pos or price <= 0:
            return False
        shares = pos["shares"] if shares is None else min(shares, pos["shares"])
        amount = shares * price
        cost = sell_cost(amount, fee_discount)
        self.cash += amount - cost
        pnl = (price - pos["avg_cost"]) * shares - cost
        self.trades.append({
            "date": str(date), "symbol": symbol, "side": "賣出", "shares": shares,
            "price": price, "amount": amount, "cost": cost, "reason": reason,
            "進場均價": pos["avg_cost"], "已實現損益": pnl,
            "報酬率(%)": (price / pos["avg_cost"] - 1) * 100,
        })
        pos["shares"] -= shares
        if pos["shares"] <= 0:
            del self.positions[symbol]
        return True

    def liquidate(self, prices: dict, date: dt.date, reason: str,
                  fee_discount: float = FEE_DISCOUNT) -> int:
        n = 0
        for sym in list(self.positions):
            px = prices.get(sym, {}).get("close")
            if px is None:
                print(f"    ⚠ {self.name}: {sym} 取不到 {date} 收盤價，無法出清，仍留在部位中")
                continue
            if self.execute_sell(sym, px, date, reason, fee_discount=fee_discount):
                n += 1
        return n


# ============================================================
# 選股規則
# ============================================================

def _clean_signals(log_path: Path, basis_date: dt.date) -> pd.DataFrame:
    """
    從 analyzer 的 log 取出指定資料基準日的訊號，並過濾掉不可用的紀錄：
      - Strategy_Decision 以 Skipped 開頭（資料不足/停滯）
      - 資料是否停滯 = True
      - 同一檔多筆時取最後一筆
    """
    df = pd.read_excel(log_path, sheet_name="分析紀錄")
    if "完整終端輸出" in df.columns:
        df = df.drop(columns=["完整終端輸出"])
    df["_基準日"] = pd.to_datetime(df["股價日期(資料基準日)"], errors="coerce").dt.date
    df["_執行時間"] = pd.to_datetime(df["執行時間"], errors="coerce")

    sub = df[df["_基準日"] == basis_date].copy()
    if sub.empty:
        return sub

    if "Strategy_Decision" in sub.columns:
        sub = sub[~sub["Strategy_Decision"].astype(str).str.startswith("Skipped")]
    if "資料是否停滯" in sub.columns:
        sub = sub[~sub["資料是否停滯"].fillna(False).astype(bool)]

    sub = sub.sort_values("_執行時間").groupby("股票代碼", as_index=False).last()
    return sub[sub["目前股價"].notna()]


def pick_targets(strategy: str, signals: pd.DataFrame, top_n: int) -> list[tuple[str, str]]:
    """回傳 [(symbol, 理由), ...]。cash 策略永遠回傳空清單。"""
    if strategy == "cash" or signals.empty:
        return []

    s = signals.copy()

    if strategy == "strategy_decision":
        picked = s[s.get("Strategy_Decision", pd.Series(dtype=object)) == "Buy"]
        return [(r["股票代碼"], f"Strategy_Decision=Buy（信心 {r.get('Strategy_Decision_信心(%)')}）")
                for _, r in picked.iterrows()][:top_n]

    if strategy == "ev_decision":
        if "EV_Decision" not in s.columns:
            return []
        picked = s[s["EV_Decision"] == "Buy"]
        return [(r["股票代碼"], f"EV_Decision=Buy（淨EV {r.get('EV_淨期望值(%)')}%）")
                for _, r in picked.iterrows()][:top_n]

    if strategy == "score_topn":
        picked = s[s["綜合分數"] > 0].sort_values("綜合分數", ascending=False).head(top_n)
        return [(r["股票代碼"], f"綜合分數 {r['綜合分數']}") for _, r in picked.iterrows()]

    if strategy == "prob_topn":
        col = "隔日_邏輯迴歸_上漲機率(%)"
        if col not in s.columns:
            return []
        picked = s[s[col].notna()].sort_values(col, ascending=False).head(top_n)
        return [(r["股票代碼"], f"隔日上漲機率 {r[col]:.1f}%") for _, r in picked.iterrows()]

    if strategy == "active_equal":
        # 分析清單本身就是活躍度篩選的結果，順序即為活躍度排名
        picked = s.head(top_n)
        return [(r["股票代碼"], "活躍度前 N 名（對照組）") for _, r in picked.iterrows()]

    return []


def pick_exits(strategy: str, signals: pd.DataFrame, positions: dict,
               targets: list[tuple[str, str]], max_holding_days: int,
               today: dt.date) -> dict:
    """
    決定今天要出場的部位。回傳 {symbol: 出場理由}。

    為什麼需要這個：原本的模擬器只買不賣（除了停損），部位會一直卡著，
    格子占滿之後就再也不會有新單。跑一週還看不出來，長期執行就會變成
    「第一天買完之後什麼都不做」——那不是任何一個策略的規則。

    出場條件分三類：
      1. 時間停損：持有超過 max_holding_days 個日曆日一律出場。
         這是防呆，避免任何部位被永久遺忘。
      2. 排名型策略（score_topn / prob_topn / active_equal）：
         跌出今日前 N 名就換掉。這些策略的規則本來就是「持有當下的
         前 N 名」，不換股就等於沒有在執行那個規則。
      3. 訊號型策略（strategy_decision / ev_decision）：
         只有出現明確的 Sell 才出場。Wait 代表模型沒有意見，
         不是叫你賣——把 Wait 當賣出訊號會造成大量無謂的來回，
         而每一次來回都要付 0.47% 成本。
    """
    exits: dict[str, str] = {}
    if not positions:
        return exits

    # 1. 時間停損（所有策略共用）
    for sym, pos in positions.items():
        try:
            held = (today - dt.date.fromisoformat(str(pos["entry_date"]))).days
        except (ValueError, TypeError):
            continue
        if held >= max_holding_days:
            exits[sym] = f"持有滿 {held} 天，時間停損"

    if signals is None or signals.empty:
        return exits

    # 2. 排名型：跌出名單就換股
    if strategy in RANKING_STRATEGIES:
        tgt = {sym for sym, _ in targets}
        for sym in positions:
            if sym not in tgt and sym not in exits:
                exits[sym] = "已跌出今日名單，換股"

    # 3. 訊號型：只認明確的 Sell
    elif strategy in SIGNAL_STRATEGIES:
        col = "Strategy_Decision" if strategy == "strategy_decision" else "EV_Decision"
        if col in signals.columns:
            sell_syms = set(signals.loc[signals[col] == "Sell", "股票代碼"])
            for sym in positions:
                if sym in sell_syms and sym not in exits:
                    exits[sym] = f"{col}=Sell"

    return exits


# ============================================================
# 模擬器
# ============================================================

class Simulator:
    def __init__(self, state: dict):
        self.state = state
        self.portfolios = {
            name: Portfolio(name, state["initial_capital"], data)
            for name, data in state["portfolios"].items()
        }

    # ---------- 建立 / 存讀 ----------
    @classmethod
    def create(cls, capital: float, top_n: int, max_position_pct: float,
               stop_loss_pct: float, stop_loss_atr: float, allow_odd_lot: bool,
               fee_discount: float, max_holding_days: int = 10) -> "Simulator":
        state = {
            "version": 1,
            "created": dt.datetime.now().isoformat(timespec="seconds"),
            "initial_capital": float(capital),
            "config": {
                "top_n": top_n,
                "max_position_pct": max_position_pct,
                "stop_loss_pct": stop_loss_pct,
                "stop_loss_atr": stop_loss_atr,
                "allow_odd_lot": allow_odd_lot,
                "fee_discount": fee_discount,
                "max_holding_days": max_holding_days,
            },
            "portfolios": {n: Portfolio(n, capital).to_dict() for n in STRATEGIES},
            "processed_dates": [],
            "settled": False,
        }
        return cls(state)

    @classmethod
    def load(cls, path: Path) -> "Simulator":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        self.state["portfolios"] = {n: p.to_dict() for n, p in self.portfolios.items()}
        path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    @property
    def cfg(self) -> dict:
        c = self.state["config"]
        c.setdefault("max_holding_days", 10)   # 舊狀態檔相容
        return c

    # ---------- 每日流程 ----------
    def step(self, date: dt.date, log_path: Path | None,
             offline_prices: pd.DataFrame | None = None,
             settle: bool = False) -> None:
        """
        單一交易日的完整流程：
          1. 用今天的開盤價撮合昨天收盤後產生的單
          2. 用今天的收盤價檢查停損
          3. 標記市值、記錄權益曲線
          4. （非結算日）用今天的分析結果產生明天的單
          5. （結算日）以今天收盤價全部出清
        """
        if str(date) in self.state["processed_dates"]:
            print(f"⚠ {date} 已處理過，略過（不重複計算）。")
            return

        print(f"\n{'='*64}\n  模擬交易日：{date}"
              f"{'（結算日）' if settle else ''}\n{'='*64}")

        # 需要取價的標的：現有部位 + 所有待成交單
        need = set()
        for p in self.portfolios.values():
            need |= set(p.positions)
            need |= {o["symbol"] for o in p.pending}

        signals = pd.DataFrame()
        if log_path is not None and log_path.exists():
            signals = _clean_signals(log_path, date)
            need |= set(signals["股票代碼"].tolist()) if not signals.empty else set()

        print(f"\n[1/4] 取得 {date} 的開盤價與收盤價（{len(need)} 檔）...")
        prices = fetch_prices(need, date, offline_prices) if need else {}
        print(f"      成功取得 {len(prices)} 檔")

        fee_d = self.cfg["fee_discount"]

        print(f"\n[2/4] 以開盤價撮合昨日產生的委託單")
        for name, p in self.portfolios.items():
            if not p.pending:
                continue
            filled, failed = 0, 0
            for order in self._sell_first(p.pending):
                px = prices.get(order["symbol"], {}).get("open")
                if px is None:
                    failed += 1
                    continue
                if order["side"] == "buy":
                    shares = self._size_position(p, order, px, prices)
                    stop_price = self._stop_price_from_fill(px, order.get("atr"))
                    if shares > 0 and p.execute_buy(order["symbol"], shares, px, date,
                                                    order["reason"], stop_price,
                                                    fee_d):
                        filled += 1
                    else:
                        failed += 1
                else:
                    if p.execute_sell(order["symbol"], px, date, order["reason"],
                                      fee_discount=fee_d):
                        filled += 1
                    else:
                        failed += 1
            print(f"      {name:18s} 成交 {filled} 筆"
                  + (f"，未成交 {failed} 筆（資金不足或取不到價）" if failed else ""))
            p.pending = []

        print(f"\n[3/4] 收盤檢查停損 / 標記市值")
        for name, p in self.portfolios.items():
            stopped = self._check_stops(p, prices, date, fee_d)
            eq = p.equity(prices)
            p.equity_curve.append({
                "date": str(date), "equity": eq, "cash": p.cash,
                "n_positions": len(p.positions),
            })
            ret = (eq / self.state["initial_capital"] - 1) * 100
            print(f"      {name:18s} 權益 {eq:>12,.0f}（{ret:+.2f}%）"
                  f" 現金 {p.cash:>11,.0f} 持股 {len(p.positions)} 檔"
                  + (f"  ⚠ 停損出場 {stopped} 檔" if stopped else ""))

        if settle:
            print(f"\n[4/4] 結算日：以 {date} 收盤價全數出清")
            for name, p in self.portfolios.items():
                n = p.liquidate(prices, date, "結算出清", fee_d)
                # 出清後權益即為現金，覆寫當日紀錄
                p.equity_curve[-1] = {"date": str(date), "equity": p.cash,
                                      "cash": p.cash, "n_positions": len(p.positions)}
                if n:
                    print(f"      {name:18s} 出清 {n} 檔 → 現金 {p.cash:,.0f}")
            self.state["settled"] = True
        else:
            print(f"\n[4/4] 依 {date} 的分析結果產生明日委託單")
            if signals.empty:
                print("      ⚠ log 中找不到這一天的分析結果（資料基準日不符），今日不產生新單。")
                print("        請確認你已在收盤後執行過 claude_stock_analyzer_v3.7。")
            else:
                print(f"      可用訊號 {len(signals)} 檔")
                for name, p in self.portfolios.items():
                    orders = self._build_orders(name, p, signals, prices, date)
                    p.pending = self._sell_first(orders)
                    if orders:
                        buys = [o["symbol"] for o in orders if o["side"] == "buy"]
                        sells = [o["symbol"] for o in orders if o["side"] == "sell"]
                        parts = []
                        if sells:
                            parts.append(f"賣出 {len(sells)} 檔（{'、'.join(sells[:4])}）")
                        if buys:
                            parts.append(f"買進 {len(buys)} 檔（{'、'.join(buys[:4])}）")
                        print(f"      {name:18s} 明日 {'，'.join(parts)}")
                    else:
                        print(f"      {name:18s} 明日無新單")

        self.state["processed_dates"].append(str(date))

    # ---------- 內部 ----------
    def _size_position(self, p: Portfolio, order: dict, price: float,
                       prices: dict) -> int:
        """依上限與現有權益決定買幾股。支援零股。"""
        equity = p.equity(prices, field="open") or self.state["initial_capital"]
        target_amount = min(
            equity * self.cfg["max_position_pct"],
            order.get("budget", equity * self.cfg["max_position_pct"]),
            p.cash * 0.995,   # 留一點緩衝給手續費
        )
        if target_amount <= 0:
            return 0
        shares = int(target_amount // price)
        if not self.cfg["allow_odd_lot"]:
            shares = (shares // SHARES_PER_LOT) * SHARES_PER_LOT
        return max(shares, 0)

    def _stop_price_from_fill(self, fill_price: float, atr: float | None) -> float:
        """
        以實際成交價為基準計算停損價，並取「百分比停損」與「ATR 停損」
        兩者中較近的那一個（較高者）。兩者都由成交價往下算，所以結果
        必定低於成交價，不會出現買進當天就被掃出場的荒謬情況。
        """
        pct_stop = fill_price * (1 - self.cfg["stop_loss_pct"])
        if atr and atr > 0:
            atr_stop = fill_price - self.cfg["stop_loss_atr"] * atr
            return max(pct_stop, atr_stop)
        return pct_stop

    def _check_stops(self, p: Portfolio, prices: dict, date: dt.date,
                     fee_discount: float) -> int:
        """
        以收盤價檢查停損。用日 K 資料只觀察得到收盤價，無法知道盤中是否
        曾經觸價——這是刻意的保守簡化：不假裝知道自己不知道的事。
        實際交易若掛停損單，出場價通常會比這裡模擬的更差。
        """
        stopped = 0
        for sym in list(p.positions):
            pos = p.positions[sym]
            px = prices.get(sym, {}).get("close")
            if px is None:
                continue
            # 舊狀態檔可能沒有 stop_price，或成交價與訊號價落差過大，
            # 一律以持有均價重算一次作為保底，確保停損線必定低於均價。
            trigger = pos.get("stop_price")
            fallback = pos["avg_cost"] * (1 - self.cfg["stop_loss_pct"])
            if trigger is None or trigger >= pos["avg_cost"]:
                trigger = fallback
            if px <= trigger:
                loss_pct = (px / pos["avg_cost"] - 1) * 100
                p.execute_sell(
                    sym, px, date,
                    f"停損觸發（收盤 {px:.2f}，停損價 {trigger:.2f}，帳面 {loss_pct:+.1f}%）",
                    fee_discount=fee_discount)
                stopped += 1
        return stopped

    def _build_orders(self, strategy: str, p: Portfolio, signals: pd.DataFrame,
                      prices: dict, today: dt.date) -> list[dict]:
        """
        產生明日的委託單。賣單排在買單前面，撮合時先賣後買，
        讓換股釋放出來的現金當天就能用。
        """
        targets = pick_targets(strategy, signals, self.cfg["top_n"])
        exits = pick_exits(strategy, signals, p.positions, targets,
                           self.cfg["max_holding_days"], today)

        orders: list[dict] = [
            {"symbol": sym, "side": "sell", "reason": reason}
            for sym, reason in exits.items()
        ]

        if not targets:
            return orders

        # 已持有且不打算賣掉的，不重複買進
        holding_after = {s for s in p.positions if s not in exits}
        targets = [(s, r) for s, r in targets if s not in holding_after]
        if not targets:
            return orders

        slots = max(self.cfg["top_n"] - len(holding_after), 0)
        targets = targets[:slots]
        if not targets:
            return orders

        equity = p.equity(prices) or self.state["initial_capital"]
        # 換股賣出後會有現金進來，所以預算用「權益 ÷ 目標檔數」而不是
        # 只看當下現金——否則換股當天會因為錢還沒回來而買不進去。
        budget = min(equity * 0.98 / max(self.cfg["top_n"], 1),
                     equity * self.cfg["max_position_pct"])

        # 注意：這裡要 append 到既有的 orders（裡面已經有賣單），
        # 不能重新指派成空 list——那會把換股的賣單整個清掉，
        # 結果就是「只買不賣、部位無限累積、還會超過 top_n 上限」。
        # 只帶 ATR 值，不預先算停損價——停損必須以「實際成交價」為基準。
        # 訊號是 D 日收盤後產生的，D+1 開盤價可能跳空，若拿 D 日收盤價去算
        # 停損線，遇到跳空就會算出高於成交價的停損價，一買進當天就被掃出場。
        atr_map = dict(zip(signals["股票代碼"], signals.get("ATR", pd.Series(dtype=float))))
        for sym, reason in targets:
            atr = atr_map.get(sym)
            orders.append({
                "symbol": sym, "side": "buy", "reason": reason, "budget": budget,
                "atr": None if atr is None or pd.isna(atr) else float(atr),
            })
        return orders

    @staticmethod
    def _sell_first(orders: list[dict]) -> list[dict]:
        return sorted(orders, key=lambda o: 0 if o.get("side") == "sell" else 1)

    # ---------- 報告 ----------
    def summary(self) -> pd.DataFrame:
        """
        尚未結算時，「期末權益」是把持股用最近一次收盤價估算的市值
        （mark-to-market），**還沒扣掉賣出時要付的手續費與證交稅**。
        所以這裡另外算一欄「若現在出清淨值」：把持股市值乘上賣出成本率
        後扣掉，那才是真的落袋數字。兩者可以差到 0.4% 以上——在一週
        報酬率本來就只有正負 1% 的尺度下，這個差距足以讓賺變成賠。
        結算後兩欄會相同（持股已全部變現）。
        """
        cap = self.state["initial_capital"]
        fee_d = self.cfg["fee_discount"]
        sell_rate = FEE_RATE * fee_d + TAX_SELL
        rows = []
        for name, p in self.portfolios.items():
            eq = p.equity_curve[-1]["equity"] if p.equity_curve else cap
            realized = sum(t.get("已實現損益", 0) for t in p.trades if t["side"] == "賣出")
            fees = sum(t["cost"] for t in p.trades)
            n_buy = sum(1 for t in p.trades if t["side"] == "買進")
            n_sell = sum(1 for t in p.trades if t["side"] == "賣出")
            wins = [t for t in p.trades if t["side"] == "賣出" and t.get("已實現損益", 0) > 0]
            curve = [e["equity"] for e in p.equity_curve]
            mdd = 0.0
            peak = cap
            for v in curve:
                peak = max(peak, v)
                mdd = min(mdd, v / peak - 1)
            # 尚未出清的持股市值 = 權益 - 現金
            holding_value = max(eq - p.cash, 0.0)
            liquidation_cost = holding_value * sell_rate if holding_value > 0 else 0.0
            net_eq = eq - liquidation_cost

            rows.append({
                "策略": name,
                "說明": STRATEGY_DESC[name],
                "期末權益": eq,
                "若現在出清淨值": net_eq,
                "出清成本估算": liquidation_cost,
                "損益": net_eq - cap,
                "報酬率(%)": (net_eq / cap - 1) * 100,
                "帳面報酬率(%)": (eq / cap - 1) * 100,
                "買進次數": n_buy,
                "賣出次數": n_sell,
                "勝率(%)": (len(wins) / n_sell * 100) if n_sell else np.nan,
                "已實現損益": realized,
                "累計交易成本": fees,
                "成本佔初始資金(%)": fees / cap * 100,
                "最大回落(%)": mdd * 100,
                "期末持股數": len(p.positions),
            })
        return pd.DataFrame(rows).sort_values("報酬率(%)", ascending=False)

    def all_trades(self) -> pd.DataFrame:
        rows = []
        for name, p in self.portfolios.items():
            for t in p.trades:
                rows.append({"策略": name, **t})
        return pd.DataFrame(rows)

    def weekly_summary(self) -> pd.DataFrame:
        """
        以「週」為單位的績效表：每一週最後一個交易日的權益，對比前一週。

        長期執行時，逐日權益曲線太細碎看不出東西，累計報酬又會被早期
        的一次大賺大賠主導。用週為單位剛好——既看得出趨勢，也還原得出
        「這一週發生了什麼」。權益一律採用扣除出清成本後的淨值，
        跟報告其他地方的口徑一致。
        """
        curves = self.all_curves()
        if curves.empty:
            return pd.DataFrame()

        c = curves.copy()
        c["_d"] = pd.to_datetime(c["date"])
        iso = c["_d"].dt.isocalendar()
        c["週別"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)

        fee_d = self.cfg["fee_discount"]
        sell_rate = FEE_RATE * fee_d + TAX_SELL
        # 週末最後一天的持股市值要扣掉出清成本才是可比較的淨值
        c["淨權益"] = c["equity"] - (c["equity"] - c["cash"]).clip(lower=0) * sell_rate

        rows = []
        cap = self.state["initial_capital"]
        for name, g in c.groupby("策略", sort=False):
            g = g.sort_values("_d")
            prev = cap
            for wk, wg in g.groupby("週別", sort=True):
                last = wg.iloc[-1]
                rows.append({
                    "週別": wk,
                    "起": wg.iloc[0]["date"],
                    "訖": last["date"],
                    "交易日數": len(wg),
                    "策略": name,
                    "週末淨權益": last["淨權益"],
                    "本週報酬(%)": (last["淨權益"] / prev - 1) * 100 if prev else np.nan,
                    "累計報酬(%)": (last["淨權益"] / cap - 1) * 100,
                    "週末持股數": int(last["n_positions"]),
                })
                prev = last["淨權益"]
        return pd.DataFrame(rows)

    def all_curves(self) -> pd.DataFrame:
        rows = []
        for name, p in self.portfolios.items():
            for e in p.equity_curve:
                rows.append({"策略": name, **e})
        return pd.DataFrame(rows)


# ============================================================
# 報告輸出
# ============================================================

def print_report(sim: Simulator) -> None:
    cap = sim.state["initial_capital"]
    settled = bool(sim.state.get("settled"))
    line = "=" * 72
    title = "紙上交易模擬 — 結算報告" if settled else "紙上交易模擬 — 期中報告（尚未結算）"
    print(f"\n{line}\n  {title}\n{line}")
    print(f"  初始資金：每個策略各 {cap:,.0f} 元（各自獨立，非平分）")
    curves = sim.all_curves()
    n_days = curves["date"].nunique() if not curves.empty else 0
    if n_days:
        print(f"  模擬期間：{curves['date'].min()} ～ {curves['date'].max()}"
              f"（已完成 {n_days} 個交易日）")
    else:
        print("  尚未執行任何交易日（剛 init 完，還沒跑過 step）")
    print(f"  已結算：{'是（持股已全部變現）' if settled else '否（持股尚未出清）'}")

    s = sim.summary()
    print(f"\n【各策略績效】")
    if settled:
        show = ["策略", "期末權益", "損益", "報酬率(%)", "買進次數", "賣出次數",
                "勝率(%)", "累計交易成本", "最大回落(%)"]
    else:
        show = ["策略", "期末權益", "若現在出清淨值", "報酬率(%)", "帳面報酬率(%)",
                "買進次數", "累計交易成本", "期末持股數"]
    print(s[show].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    if not settled and (s["出清成本估算"] > 0).any():
        print("\n  ※「期末權益」是持股按最近收盤價估算的市值，還沒扣掉賣出時要付的")
        print("     手續費與證交稅。「若現在出清淨值」才是真的落袋數字，")
        print("     「報酬率(%)」也是以它計算。兩者可以差 0.4% 以上——在一週報酬率")
        print("     本來就只有正負 1% 的尺度下，這個差距足以讓帳面的賺變成實際的賠。")

    # 長期執行時，週轉率是最容易被忽略、也最容易致命的一項
    heavy = s[(s["成本佔初始資金(%)"] > 1.0)]
    if len(heavy) and n_days:
        print(f"\n  ⚠ 交易成本警示（累計成本已超過本金 1%）：")
        for _, r in heavy.sort_values("成本佔初始資金(%)", ascending=False).iterrows():
            per_year = r["成本佔初始資金(%)"] / n_days * 250
            print(f"    {r['策略']:18s} 累計成本 {r['累計交易成本']:>10,.0f} 元"
                  f"（本金的 {r['成本佔初始資金(%)']:.2f}%），"
                  f"以目前週轉速度年化約 {per_year:.1f}%")
        print(f"    台股來回一趟 {round_trip_cost_pct():.2f}%，換股越勤成本吃得越兇。")
        print(f"    若某策略報酬贏不過它自己的成本，那個規則就是在幫券商賺錢。")

    cash_row = s[s["策略"] == "cash"]
    if not cash_row.empty:
        base = float(cash_row["報酬率(%)"].iloc[0])
        print(f"\n【對照：什麼都不做 = {base:+.2f}%】")
        others = s[s["策略"] != "cash"]
        beat = others[others["報酬率(%)"] > base + 1e-9]
        tie = others[(others["報酬率(%)"] - base).abs() <= 1e-9]
        lose = others[others["報酬率(%)"] < base - 1e-9]
        print(f"  贏過：{len(beat)} 個"
              + (f"（{'、'.join(beat['策略'])}）" if len(beat) else ""))
        if len(tie):
            print(f"  打平：{len(tie)} 個（{'、'.join(tie['策略'])}）"
                  + "　← 完全沒有交易，所以跟全現金一樣")
        print(f"  輸給：{len(lose)} 個"
              + (f"（{'、'.join(lose['策略'])}）" if len(lose) else ""))

    weekly = sim.weekly_summary()
    if not weekly.empty and weekly["週別"].nunique() >= 1:
        print(f"\n【週績效】（每週最後一個交易日的淨權益，已扣出清成本）")
        pivot = weekly.pivot(index="週別", columns="策略", values="本週報酬(%)")
        order = [c for c in STRATEGIES if c in pivot.columns]
        print(pivot[order].to_string(float_format=lambda v: f"{v:+.2f}"))
        last_wk = weekly["週別"].max()
        lw = weekly[weekly["週別"] == last_wk].sort_values("本週報酬(%)", ascending=False)
        print(f"\n  最近一週（{last_wk}，{lw.iloc[0]['起']} ~ {lw.iloc[0]['訖']}，"
              f"{lw.iloc[0]['交易日數']} 個交易日）排名：")
        for _, r in lw.iterrows():
            print(f"    {r['策略']:18s} 本週 {r['本週報酬(%)']:+6.2f}%"
                  f"　累計 {r['累計報酬(%)']:+6.2f}%　持股 {r['週末持股數']} 檔")

    trades = sim.all_trades()
    if not trades.empty:
        print(f"\n【交易明細】共 {len(trades)} 筆")
        cols = [c for c in ["策略", "date", "symbol", "side", "shares", "price",
                            "cost", "報酬率(%)", "reason"] if c in trades.columns]
        shown = trades.sort_values("date").tail(25)
        if len(trades) > 25:
            print(f"  （只列最近 25 筆，完整明細請用 -o 匯出 Excel）")
        print(shown[cols].to_string(index=False, max_colwidth=32))
    else:
        print(f"\n【交易明細】整個模擬期間沒有任何交易。")
        print("  如果這是 strategy_decision 造成的，那正是這次模擬要看到的結果：")
        print("  現行規則在這段期間完全不出手。")

    print(f"\n{line}")
    print("【判讀提醒 — 請務必看完再下結論】")
    if n_days == 0:
        print("  0. 目前還沒有執行任何交易日，以上數字全部是初始值。")
        print("     請先執行 tw_stock_pipeline_v1.1.py 或 paper_trading.py step。")
    print(f"  1. 本次模擬只有 {n_days} 個交易日。同一天不同股票會一起漲跌，")
    print(f"     有效獨立樣本數大約是 1，不是 {n_days}，更不是交易筆數。")
    print("  2. 這個結果無法區分「策略有效」與「這週運氣好」。名次高低幾乎")
    print("     完全由這一週的大盤方向決定。")
    print("  3. 唯一能從一週得到的可靠結論，是流程上的：資料抓得到嗎？")
    print("     訊號有出現嗎？100 萬買得起嗎？成本吃掉多少？")
    print(f"  4. 台股來回交易成本約 {round_trip_cost_pct():.2f}%。若某策略報酬率")
    print("     落在正負 1% 內，那個數字基本上就是成本與雜訊。")
    print("  5. 要判斷策略好壞，請看 tools/log_review.py 累積數個月後的統計。")
    print(f"{line}")
    print("本模擬為紙上推演，不下真單，不構成投資建議。\n")


def write_report(sim: Simulator, path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        sim.summary().to_excel(w, sheet_name="策略績效", index=False)
        trades = sim.all_trades()
        if not trades.empty:
            trades.to_excel(w, sheet_name="交易明細", index=False)
        weekly = sim.weekly_summary()
        if not weekly.empty:
            weekly.to_excel(w, sheet_name="週績效", index=False)
            weekly.pivot(index="週別", columns="策略",
                         values="本週報酬(%)").to_excel(w, sheet_name="週報酬對照")
        curves = sim.all_curves()
        if not curves.empty:
            curves.to_excel(w, sheet_name="每日權益", index=False)
            pivot = curves.pivot(index="date", columns="策略", values="equity")
            pivot.to_excel(w, sheet_name="權益曲線")
        pd.DataFrame([{"參數": k, "值": v} for k, v in sim.cfg.items()]
                     + [{"參數": "初始資金", "值": sim.state["initial_capital"]},
                        {"參數": "已結算", "值": sim.state.get("settled")}]
                     ).to_excel(w, sheet_name="模擬參數", index=False)
    print(f"報告已輸出：{path}")


# ============================================================
# CLI
# ============================================================

def _print_usage_guide(state_path: Path) -> None:
    """
    沒有帶子指令時印出的說明。直接用 VS Code 的 ▶ Run 按鈕執行會走到這裡，
    所以這裡要印出「可以直接複製貼上」的完整指令，而不是抽象的用法字串。
    """
    exe = sys.executable
    me = Path(__file__).resolve()
    prefix = f'& "{exe}" "{me}"' if " " in exe or " " in str(me) else f"{exe} {me}"

    print("=" * 70)
    print("  台股紙上交易模擬器 — 這支程式需要指定「要做什麼」")
    print("=" * 70)
    print("\n這不是錯誤，只是你還沒告訴它要執行哪個動作。")
    print("VS Code 右上角的 ▶ Run 按鈕不會帶參數，所以請改用終端機輸入指令。")

    exists = state_path.exists()
    print(f"\n目前模擬狀態檔：{state_path}")
    print(f"  {'✓ 已存在，可以直接推進交易日' if exists else '✗ 尚未建立，請先執行下面的 init'}")

    print("\n" + "-" * 70)
    if not exists:
        print("【下一步：建立模擬】把下面這一整行複製到終端機執行\n")
        print(f"  {prefix} init --capital 1000000\n")
    else:
        print("【下一步：每天收盤後推進一個交易日】\n")
        print(f"  {prefix} step --date {dt.date.today().isoformat()}\n")
        print("  （通常不必手動下這一行，執行 tw_stock_pipeline_v1.1.py 會自動呼叫）\n")
    print("-" * 70)

    print("\n所有可用的子指令：")
    for cmd, desc in (
        ("init", "建立新的模擬（100 萬 × 6 個獨立策略帳戶），只需執行一次"),
        ("step", "推進一個交易日：撮合昨日委託、標記市值、產生明日委託"),
        ("settle", "最後一天結算：全數出清並印出完整報告"),
        ("report", "印出目前績效報告，可加 -o 檔名.xlsx 匯出"),
        ("status", "查看目前現金、持股與待成交委託單"),
    ):
        print(f"  {cmd:8s} {desc}")

    print("\n常用範例：")
    print(f"  {prefix} init --capital 1000000")
    print(f"  {prefix} status")
    print(f"  {prefix} report -o 模擬結算報告.xlsx")
    print(f"  {prefix} settle --date 2026-09-04")

    print("\n若想少打一點字，可以先切換到程式所在資料夾：")
    print(f"  cd \"{me.parent}\"")
    print(f"  python {me.name} init --capital 1000000")
    print("\n完整說明：docs/模擬投資使用說明.md")
    print("=" * 70)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="台股紙上交易模擬器")
    ap.add_argument("--state", type=Path, default=None, help="狀態檔路徑")
    # 刻意不設 required=True：直接用 VS Code 的 ▶ Run 按鈕執行時不會帶參數，
    # argparse 預設的錯誤訊息（"the following arguments are required: cmd"）
    # 對不熟指令列的使用者幫助有限。改成印出可直接複製的完整指令。
    sub = ap.add_subparsers(dest="cmd", required=False)

    def _add_state(parser):
        # --state 放在子指令前後都能用，免得每次都要記位置
        parser.add_argument("--state", type=Path, default=None, help="狀態檔路徑")

    p_init = sub.add_parser("init", help="建立新的模擬")
    _add_state(p_init)
    p_init.add_argument("--capital", type=float, default=1_000_000)
    p_init.add_argument("--top-n", type=int, default=5, help="每個策略最多同時持有幾檔")
    p_init.add_argument("--max-position-pct", type=float, default=0.25,
                        help="單一標的佔權益上限")
    p_init.add_argument("--stop-loss-pct", type=float, default=0.07)
    p_init.add_argument("--stop-loss-atr", type=float, default=2.0)
    p_init.add_argument("--lot-only", action="store_true",
                        help="只買整張（1000股）。預設允許零股，因為 100 萬買不起一張台積電")
    p_init.add_argument("--fee-discount", type=float, default=FEE_DISCOUNT)
    p_init.add_argument("--max-holding-days", type=int, default=10,
                        help="最長持有天數（日曆日），超過一律出場。防止部位被永久遺忘")

    for name, helptext in (("step", "執行一個交易日"), ("settle", "執行最後一天並結算出清")):
        pp = sub.add_parser(name, help=helptext)
        pp.add_argument("--date", required=True, help="交易日 YYYY-MM-DD")
        pp.add_argument("--log", type=Path, default=Path("stock_analysis_log_v3.7.xlsx"))
        pp.add_argument("--offline-prices", type=Path, default=None,
                        help="測試用：含 date/symbol/open/close 的 CSV")
        _add_state(pp)

    p_rep = sub.add_parser("report", help="輸出結算報告")
    p_rep.add_argument("-o", "--output", type=Path, default=None)
    _add_state(p_rep)

    p_st = sub.add_parser("status", help="看目前部位與待成交單")
    _add_state(p_st)

    args = ap.parse_args(argv)
    if getattr(args, "state", None) is None:
        args.state = DEFAULT_STATE

    if args.cmd is None:
        _print_usage_guide(args.state)
        return 0

    if args.cmd == "init":
        if args.state.exists():
            print(f"⚠ {args.state} 已存在。若要重新開始，請先刪除或改用 --state 指定新檔名。")
            return 1
        sim = Simulator.create(args.capital, args.top_n, args.max_position_pct,
                               args.stop_loss_pct, args.stop_loss_atr,
                               not args.lot_only, args.fee_discount,
                               args.max_holding_days)
        sim.save(args.state)
        print(f"✓ 已建立模擬：{args.state}")
        print(f"  初始資金：{args.capital:,.0f} 元 × {len(STRATEGIES)} 個獨立策略帳戶")
        print(f"  每帳戶最多持有 {args.top_n} 檔，單一標的上限 {args.max_position_pct:.0%}")
        print(f"  最長持有 {args.max_holding_days} 天；排名型策略跌出名單即換股")
        print(f"  無結束日期，持續執行到你自己下 settle 為止")
        print(f"  零股交易：{'否（只買整張）' if args.lot_only else '是'}")
        print(f"  台股來回成本：約 {round_trip_cost_pct(args.fee_discount):.3f}%")
        print(f"\n策略清單：")
        for n in STRATEGIES:
            print(f"  {n:18s} {STRATEGY_DESC[n]}")
        return 0

    if not args.state.exists():
        print(f"找不到狀態檔 {args.state}，請先執行 init。")
        return 1
    sim = Simulator.load(args.state)

    if args.cmd in ("step", "settle"):
        date = dt.date.fromisoformat(args.date)
        offline = None
        if args.offline_prices:
            offline = pd.read_csv(args.offline_prices)
        log = args.log if args.log and args.log.exists() else None
        if log is None and args.cmd == "step":
            print(f"⚠ 找不到分析紀錄檔 {args.log}，本日只會撮合既有委託單、不產生新單。")
        sim.step(date, log, offline, settle=(args.cmd == "settle"))
        sim.save(args.state)
        if args.cmd == "settle":
            print_report(sim)
        return 0

    if args.cmd == "report":
        print_report(sim)
        if args.output:
            write_report(sim, args.output)
        return 0

    if args.cmd == "status":
        print(f"初始資金：{sim.state['initial_capital']:,.0f}／策略")
        print(f"已處理交易日：{sim.state['processed_dates']}")
        for name, p in sim.portfolios.items():
            print(f"\n[{name}] 現金 {p.cash:,.0f}")
            for sym, pos in p.positions.items():
                print(f"    {sym}: {pos['shares']} 股 @ {pos['avg_cost']:.2f}"
                      f"（{pos['entry_date']} 進場）")
            if p.pending:
                print(f"    待成交：{[o['symbol'] for o in p.pending]}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
