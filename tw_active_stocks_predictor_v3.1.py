# -*- coding: utf-8 -*-
"""
台股活躍股預測程式 v3.1
========================
v3.0 -> v3.1 修正（皆為 v2.1 就存在、v3.0 沿用下來的問題）：

  [重大] 網路錯誤被誤報為「假日」。連不上 TWSE 時，舊版一律印
         「無資料（可能為假日/停市/欄位異常），略過」，然後繼續重試——
         回看 30 天最多會試 110 次、每次逾時 15 秒，等於在使用者網路
         斷線的情況下花約 30 分鐘印出上百行誤導訊息才失敗。
         v3.1 把「連不上」與「當天沒有資料」分成兩件事：前者丟出
         NetworkUnavailable，連續 3 次即停止並直接指出是網路問題、
         附上檢查步驟；後者才是假日，維持原本的略過邏輯。
         同時把冗長的 requests 例外訊息壓成一句人看得懂的話。

  [新增] 自動切換 TWSE 主機。實際遇過「瀏覽器開得了 twse.com.tw，
         Python 卻在 www.twse.com.tw 上 DNS 解析失敗」——某些企業
         DNS 或 VPN 只解析得到其中一個。改為依序嘗試 TWSE_HOSTS
         清單中的主機，成功一次之後就固定用它，不再重試失敗的那個。

  [重大] 抓取起點永遠從「昨天」開始，導致最新一個交易日的資料拿得到
         卻沒被使用。TWSE 在收盤後（約 14:00 起）就會發布當日 MI_INDEX，
         所以收盤後執行時，v2.1/v3.0 分析的是「截至昨天」的活躍度，
         再用它去「預測明天」——中間整整跳過了今天。接進每日 pipeline
         時，篩選端用的是 D-1 的資料，分析端（analyzer）用的卻是 D 的
         資料，兩邊差一天。
         v3.1 改為：現在時間 >= 14:00 就從今天開始試抓；資料尚未發布時
         TWSE 會回傳 stat != "OK"，既有的 None 處理會自動跳過，不會出錯。

  [修正] 4 碼規則擋不掉 ETF。台灣 ETF 代號 0050(元大台灣50)、0056
         (元大高股息)、0051、0052、0057 等都是 4 碼純數字，會直接通過
         v3.0 的「只留 4 碼」過濾——而 0050 與 0056 正是全市場成交最熱
         的標的之一，活躍度排名幾乎一定會抓到。v3.1 另外排除以 "00"
         開頭的代號（上市普通股代號範圍 1101~9958，開頭一律是 1~9）。

  [修正] 名稱關鍵字「期」會誤殺合法上市普通股。實測：群益期(6024) 是
         上市期貨商，被「期」排除。改用「期信」「期貨信託」等更精確的
         字樣，並實測確認華票、台新金、宏遠證、台中銀等不受影響。

  [透明度] 分數為 NaN 的標的（成交金額等欄位在 TWSE 原始資料是 "--"）
         原本會靜靜沉到排序最後，不會有任何提示。現在會回報筆數，
         讓你知道有幾檔因為資料缺漏而未被評分。

v3.0 改良重點（以下沿用）：
1. 僅使用證交所官方 API（TWSE）
2. 排除 ETF / ETN / 權證 / 指數商品（以代碼規則初步過濾）
3. 回看天數拉長，降低短樣本噪音
4. 對極端值做 log + clipping，避免量比暴衝扭曲排序
5. 設定最低歷史樣本門檻，避免資料不足股票進榜
6. 預測採用「最近平均分數 + 趨勢斜率 * 趨勢可信度(r2)」
   比單純 latest + slope 更穩健
7. Excel 輸出包含更多解釋欄位

注意：
- 本程式仍屬「活躍度排序/觀察工具」，不是投資報酬預測模型
- 不構成任何投資建議
"""

import time
import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ============================================================
# 參數設定區
# ============================================================

LOOKBACK_DAYS = 30          # 回看近幾個有效交易日
MIN_HISTORY_DAYS = 15       # 至少需要幾天資料才納入預測
TOP_N = 10                  # 輸出前幾名
ROLLING_BASE_DAYS = 10      # 量比比較基準：前 N 日均量
RECENT_AVG_DAYS = 3         # 最近幾日平均分數

# 活躍度分數權重（總和建議 = 1.0）
WEIGHT_TRADING_MONEY = 0.40
WEIGHT_VOLUME_RATIO = 0.25
WEIGHT_TURNOVER_COUNT = 0.20
WEIGHT_AMPLITUDE = 0.15

# clipping 分位數，降低極端值影響
CLIP_LOWER_Q = 0.01
CLIP_UPPER_Q = 0.99

OUTPUT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = OUTPUT_DIR / f"台股活躍股預測_v3.1_{datetime.date.today().isoformat()}.xlsx"

# TWSE 的 API 主機。以 www 開頭的那個是官方文件與長年使用的位址，
# 但實際遇過「瀏覽器開得了 twse.com.tw，Python 卻在 www.twse.com.tw 上
# DNS 解析失敗」的情況——某些企業 DNS 或 VPN 只解析得到其中一個。
# 因此改為依序嘗試；第一個成功的會被記住，之後不再重試前面失敗的。
TWSE_HOSTS = ["www.twse.com.tw", "twse.com.tw"]

# TWSE 近年把網站改版成 RWD 架構，舊的 /exchangeReport/... 路徑正在陸續
# 搬到 /rwd/zh/afterTrading/...。改版是分批進行的，所以同一時間可能兩個
# 都還能用、也可能舊的突然停掉。這裡依序嘗試，哪個回得出 JSON 就用哪個。
#
# ⚠ 新路徑是依 TWSE 改版慣例列入的備援，未在本機實測過。若舊路徑失效
#   而新路徑也不通，請自行到 TWSE 網站查目前的端點並更新這份清單——
#   程式會明確告訴你是「連不上」還是「端點回應不對」，不會混為一談。
TWSE_PATHS = [
    "/exchangeReport/MI_INDEX",          # 長年使用的路徑
    "/rwd/zh/afterTrading/MI_INDEX",     # 改版後的路徑（備援）
]
TWSE_PATH = TWSE_PATHS[0]
TWSE_URL = f"https://{TWSE_HOSTS[0]}{TWSE_PATH}"   # 相容舊程式碼的引用

_WORKING_ENDPOINT = None   # (host, path)：成功一次之後就固定用它
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
# 工具函式
# ============================================================

def zscore(s: pd.Series) -> pd.Series:
    """標準化：(x - mean) / std，std=0 時回傳全 0"""
    s = pd.to_numeric(s, errors="coerce")
    std = s.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / std


def clip_series(s: pd.Series, lower_q=CLIP_LOWER_Q, upper_q=CLIP_UPPER_Q) -> pd.Series:
    """依分位數裁剪極端值"""
    s = pd.to_numeric(s, errors="coerce")
    low = s.quantile(lower_q)
    high = s.quantile(upper_q)
    return s.clip(lower=low, upper=high)


def safe_log1p(s: pd.Series) -> pd.Series:
    """非負數取 log1p，若有負值先轉 NaN"""
    s = pd.to_numeric(s, errors="coerce")
    s = s.where(s >= 0, np.nan)
    return np.log1p(s)


def calc_r2(x: np.ndarray, y: np.ndarray, slope: float, intercept: float) -> float:
    """計算線性回歸擬合度 R^2"""
    y_pred = slope * x + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    if ss_tot == 0:
        return 0.0
    return max(0.0, 1 - ss_res / ss_tot)


def is_common_stock(stock_id: str, stock_name: str = "") -> bool:
    """
    以代碼規則初步過濾：
    只保留較像一般上市普通股的標的，排除 ETF / ETN / 權證 / 指數商品 / 特殊商品
    說明：
    - 台股一般上市普通股為 4 位數字且開頭 1~9，例如 2330、2454、2301
    - ETF 有 4 碼的（0050、0056、0051）也有 5~6 碼的（006208），
      共同點是以 "00" 開頭，所以用開頭字元排除比用長度可靠
    - ETN 常見 020xxx
    - 權證常為 6 碼以上且命名有特徵
    """
    sid = str(stock_id).strip()
    name = str(stock_name).strip()

    if not sid.isdigit():
        return False

    # 普通股通常是 4 碼
    if len(sid) != 4:
        return False

    # v3.1 修正：光是「4 碼」擋不掉 ETF。台灣 ETF 代號 0050(元大台灣50)、
    # 0056(元大高股息)、0051、0052、0057 等都是 4 碼，而且 0050 與 0056
    # 是全市場成交最熱的標的之一——活躍度排名幾乎一定會抓到它們，
    # 卻會直接通過 v3.0 的「只留 4 碼」規則。
    # 上市普通股代號範圍是 1101~9958，開頭一律是 1~9；以 "00" 開頭的
    # 都是 ETF/ETN 等指數化商品。
    if sid.startswith("00"):
        return False

    # 名稱再做一層保護性排除。
    # v3.1 修正：原本清單含單字「期」，會誤殺合法上市普通股——實測
    # 群益期(6024) 這家上市期貨商就被排除掉了。改用更精確的字樣，
    # 並實測確認華票、台新金、宏遠證、台中銀、潤泰新等不受影響。
    excluded_keywords = [
        "ETF", "ETN", "權證", "反1", "正2", "槓桿", "反向", "特別股",
        "期信", "期貨信託", "指數", "存託",
    ]
    if any(k in name for k in excluded_keywords):
        return False

    return True


# ============================================================
# 資料抓取（證交所官方公開 API）
# ============================================================

class NetworkUnavailable(Exception):
    """連不上 TWSE。這與「當天是假日沒有資料」完全是兩回事，必須分開處理。"""


def _short_network_reason(exc: Exception) -> str:
    """把冗長的 requests 例外訊息壓成一句人看得懂的話。"""
    text = str(exc)
    if "getaddrinfo failed" in text or "NameResolutionError" in text:
        return "DNS 解析失敗——連網域名稱都查不到，通常是網路斷線或 DNS 設定有問題"
    if "10065" in text or "unreachable" in text.lower():
        return "無法路由到主機——網路可通但到不了 TWSE，常見於 VPN 或公司防火牆"
    if "timed out" in text.lower() or "ConnectTimeout" in text:
        return "連線逾時——可能被防火牆或 proxy 擋住"
    if "SSLError" in text or "certificate" in text.lower():
        return "TLS 憑證驗證失敗——通常是公司網路的中間人代理"
    return text.split("(Caused by")[0].strip()[:120]


def fetch_twse_day(date_str: str) -> pd.DataFrame:
    """
    抓取指定日期（YYYYMMDD）全部上市股票的日成交資料。

    回傳 None 代表「當天沒有資料」（假日、停市）。
    連不上 TWSE 時會丟出 NetworkUnavailable，不會回傳 None——
    這兩件事必須分開：舊版把網路錯誤也印成「無資料（可能為假日）」，
    等於在使用者網路斷線時告訴他今天是假日，然後繼續重試上百次。
    """
    global _WORKING_ENDPOINT
    params = {"response": "json", "date": date_str, "type": "ALLBUT0999"}

    # 已經找到可用端點就只試那一個；否則主機 × 路徑逐一嘗試
    if _WORKING_ENDPOINT:
        candidates = [_WORKING_ENDPOINT]
    else:
        candidates = [(h, p) for h in TWSE_HOSTS for p in TWSE_PATHS]

    last_network_error = None
    http_errors = []
    data = None

    for host, path in candidates:
        url = f"https://{host}{path}"
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        except requests.exceptions.RequestException as e:
            last_network_error = e
            continue

        # 4xx/5xx 代表「連得上但這個端點不對」，與網路不通是兩回事，
        # 應該換下一個端點試，而不是判定成網路故障。
        if resp.status_code >= 400:
            http_errors.append(f"{url} → HTTP {resp.status_code}")
            continue
        try:
            data = resp.json()
        except Exception:
            http_errors.append(f"{url} → 回應不是 JSON（可能被導向網頁版）")
            continue

        if _WORKING_ENDPOINT != (host, path):
            _WORKING_ENDPOINT = (host, path)
            print(f"    · 使用端點 https://{host}{path}")
        break
    else:
        if last_network_error is not None and not http_errors:
            raise NetworkUnavailable(_short_network_reason(last_network_error)) from last_network_error
        detail = "；".join(http_errors[:4])
        raise NetworkUnavailable(
            f"連得上 TWSE，但所有已知端點都回應不正確：{detail}。"
            f"這通常代表 TWSE 改版換了網址，請更新程式裡的 TWSE_PATHS")

    if data.get("stat") != "OK":
        return None

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
            .replace({"--": np.nan, "---": np.nan, "": np.nan})
        )
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["date"] = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]

    # 只保留一般普通股，先排除 ETF / ETN / 權證等
    df = df[df.apply(lambda r: is_common_stock(r["stock_id"], r["stock_name"]), axis=1)]

    return df.reset_index(drop=True)


# TWSE 在收盤後才會發布當日 MI_INDEX，這個時間點之後才值得嘗試抓「今天」
TWSE_PUBLISH_TIME = datetime.time(14, 0)


def data_start_date(now: datetime.datetime = None) -> datetime.date:
    """
    決定往前抓的起點。

    v3.1 修正：v2.1 與 v3.0 都寫死 `today() - 1 day`，等於永遠不看今天。
    但 TWSE 收盤後就會發布當日資料，所以收盤後執行時，最新一個交易日
    是拿得到卻被跳過的——分析出來的「明日活躍度」其實預測的是今天，
    而今天已經結束了。

    14:00 之後就從今天開始試。若當日資料尚未發布，TWSE 會回傳
    stat != "OK"，fetch_twse_day() 既有的 None 處理會自動往前跳，
    不需要額外處理，也不會出錯。
    """
    now = now or datetime.datetime.now()
    if now.time() >= TWSE_PUBLISH_TIME:
        return now.date()
    return now.date() - datetime.timedelta(days=1)


def fetch_panel_data(n_days: int, now: datetime.datetime = None) -> pd.DataFrame:
    """從最近可用的交易日開始往前找，直到湊滿 n_days 個有效交易日資料"""
    frames = []
    current = data_start_date(now)
    attempts = 0
    max_attempts = n_days * 3 + 20

    # 連續網路失敗的容忍次數。超過就直接放棄——與其花 30 分鐘重試 110 次
    # 再告訴使用者失敗，不如 1 分鐘內講清楚「你連不上網」。
    MAX_CONSECUTIVE_NETWORK_FAILURES = 3
    net_fail_streak = 0
    last_net_reason = ""

    while len(frames) < n_days and attempts < max_attempts:
        attempts += 1

        if current.weekday() < 5:
            date_str = current.strftime("%Y%m%d")
            print(f"  嘗試抓取 {date_str} ...")
            try:
                df = fetch_twse_day(date_str)
            except NetworkUnavailable as e:
                net_fail_streak += 1
                last_net_reason = str(e)
                print(f"    ✗ 連線失敗（{net_fail_streak}/{MAX_CONSECUTIVE_NETWORK_FAILURES}）：{e}")
                if net_fail_streak >= MAX_CONSECUTIVE_NETWORK_FAILURES:
                    tried = "".join(f"      https://{h}{p}\n"
                                    for h in TWSE_HOSTS for p in TWSE_PATHS)
                    raise RuntimeError(
                        f"連續 {net_fail_streak} 次連不上 TWSE，停止重試。\n"
                        f"  原因：{last_net_reason}\n"
                        f"  已嘗試的端點：\n{tried}"
                        f"\n  這不是假日、也不是程式的問題。請依序確認\n"
                        f"  （注意有沒有 www，兩者可能不同）：\n"
                        f"    1. 瀏覽器貼上這個完整網址，看是否出現 JSON：\n"
                        f"       https://www.twse.com.tw/exchangeReport/MI_INDEX"
                        f"?response=json&date=20260902&type=ALLBUT0999\n"
                        f"    2. Resolve-DnsName www.twse.com.tw\n"
                        f"    3. Test-NetConnection www.twse.com.tw -Port 443\n"
                        f"    4. VPN 是否剛連上或斷線、公司 proxy 是否擋住\n"
                        f"\n  若第 1 步看得到 JSON、程式卻連不上，多半是 proxy 只放行\n"
                        f"  瀏覽器而擋住 Python。網路恢復後重跑即可，紀錄不受影響。"
                    ) from None
                time.sleep(2.0)
                current -= datetime.timedelta(days=1)
                continue

            net_fail_streak = 0     # 有成功連上就重置
            if df is not None and not df.empty:
                print(f"    OK，取得 {len(df)} 檔普通股資料")
                frames.append(df)
            else:
                print("    無資料（假日/停市），略過")

            time.sleep(1.0)

        current -= datetime.timedelta(days=1)

    if len(frames) < n_days:
        print(f"  警告：只湊到 {len(frames)} 個有效交易日（原訂 {n_days} 天），將以實際天數繼續分析")

    if not frames:
        raise RuntimeError("完全抓不到任何交易日資料，請檢查網路連線或證交所服務狀態")

    frames = list(reversed(frames))
    panel = pd.concat(frames, ignore_index=True)

    # 防止同日重複
    panel = panel.drop_duplicates(subset=["date", "stock_id"]).reset_index(drop=True)
    return panel


# ============================================================
# 活躍度計算
# ============================================================

def compute_daily_scores(panel: pd.DataFrame) -> pd.DataFrame:
    """
    對每個交易日，計算每檔股票的活躍指標：
    1. 成交值（log後）
    2. 量比（相對前 N 日均量，log後）
    3. 成交筆數（log後）
    4. 振幅

    然後在每日截面上標準化，加權合成 activity_score
    """
    df = panel.copy()
    df = df.sort_values(["stock_id", "date"]).reset_index(drop=True)

    # 振幅
    mid = (df["max"] + df["min"]) / 2
    mid = mid.replace(0, np.nan)
    df["amplitude"] = (df["max"] - df["min"]) / mid
    df["amplitude"] = df["amplitude"].replace([np.inf, -np.inf], np.nan).fillna(0)

    # 前N日均量（不含當日）
    df["volume_ma_prior"] = (
        df.groupby("stock_id")["Trading_Volume"]
        .transform(lambda s: s.shift(1).rolling(ROLLING_BASE_DAYS, min_periods=3).mean())
    )

    # 今日量 / 前均量
    df["volume_ratio"] = df["Trading_Volume"] / df["volume_ma_prior"]
    df["volume_ratio"] = df["volume_ratio"].replace([np.inf, -np.inf], np.nan)
    df["volume_ratio"] = df["volume_ratio"].fillna(1.0)

    scored_frames = []

    for d, day_df in df.groupby("date"):
        day_df = day_df.copy()

        # 極端值處理 + log 轉換
        day_df["money_feat"] = safe_log1p(clip_series(day_df["Trading_money"]))
        day_df["turnover_feat"] = safe_log1p(clip_series(day_df["Trading_turnover"]))
        day_df["vratio_feat"] = np.log(day_df["volume_ratio"].clip(lower=0.2, upper=5.0))
        day_df["amplitude_feat"] = clip_series(day_df["amplitude"], 0.01, 0.99)

        # 再做 z-score
        day_df["z_money"] = zscore(day_df["money_feat"])
        day_df["z_volume_ratio"] = zscore(day_df["vratio_feat"])
        day_df["z_turnover_count"] = zscore(day_df["turnover_feat"])
        day_df["z_amplitude"] = zscore(day_df["amplitude_feat"])

        # 加權活躍度分數
        day_df["activity_score"] = (
            WEIGHT_TRADING_MONEY * day_df["z_money"]
            + WEIGHT_VOLUME_RATIO * day_df["z_volume_ratio"]
            + WEIGHT_TURNOVER_COUNT * day_df["z_turnover_count"]
            + WEIGHT_AMPLITUDE * day_df["z_amplitude"]
        )

        scored_frames.append(day_df)

    return pd.concat(scored_frames, ignore_index=True)


# ============================================================
# 預測/排序
# ============================================================

def predict_next_day_activity(scored: pd.DataFrame) -> pd.DataFrame:
    """
    改良版：
    - 不再直接用 latest_score + slope
    - 改用：
        recent_avg_score = 最近幾日平均分數
        slope = 線性回歸斜率
        r2 = 趨勢擬合度
        trend_strength = r2
        predicted_next_score = recent_avg_score + slope * trend_strength

    這樣做的原因：
    - recent_avg_score 比單日 latest_score 穩
    - slope 只有在擬合度高時才有較大權重
    """
    results = []

    for stock_id, g in scored.groupby("stock_id"):
        g = g.sort_values("date").reset_index(drop=True)

        if len(g) < MIN_HISTORY_DAYS:
            continue

        y = g["activity_score"].values.astype(float)
        x = np.arange(len(g), dtype=float)

        if len(y) < 2:
            continue

        slope, intercept = np.polyfit(x, y, 1)
        r2 = calc_r2(x, y, slope, intercept)
        trend_strength = max(0.0, min(1.0, r2))

        latest_score = float(y[-1])
        recent_avg_score = float(np.mean(y[-RECENT_AVG_DAYS:]))

        # 比較穩健的外推
        predicted_score = recent_avg_score + slope * trend_strength

        results.append(
            {
                "stock_id": stock_id,
                "stock_name": g["stock_name"].iloc[-1],
                "predicted_next_score": predicted_score,
                "recent_avg_score": recent_avg_score,
                "latest_score": latest_score,
                "trend_slope": slope,
                "trend_r2": r2,
                "trend_strength": trend_strength,
                "days_used": len(g),
                "latest_trading_money": g["Trading_money"].iloc[-1],
                "latest_volume": g["Trading_Volume"].iloc[-1],
                "latest_volume_ratio": g["volume_ratio"].iloc[-1],
                "latest_amplitude": g["amplitude"].iloc[-1],
                "latest_date": g["date"].iloc[-1],
            }
        )

    result_df = pd.DataFrame(results)

    if result_df.empty:
        return result_df

    # v3.1：分數為 NaN 的標的（TWSE 原始資料該欄是 "--"）原本會靜靜沉到
    # 排序最後，不會有任何提示。這裡回報筆數，讓使用者知道有幾檔是因為
    # 資料缺漏而未被評分，而不是因為活躍度低。
    n_nan = int(result_df["predicted_next_score"].isna().sum())
    if n_nan:
        nan_ids = result_df.loc[result_df["predicted_next_score"].isna(),
                                "stock_id"].tolist()
        print(f"  註：{n_nan} 檔因原始資料有缺漏（成交金額/筆數為 '--'）而無法評分，"
              f"已排除於排名之外：{nan_ids[:10]}{' ...' if n_nan > 10 else ''}")
        result_df = result_df[result_df["predicted_next_score"].notna()]

    # 排序優先：預測分數，再看 recent_avg_score
    result_df = result_df.sort_values(
        ["predicted_next_score", "recent_avg_score"],
        ascending=[False, False]
    ).reset_index(drop=True)

    return result_df


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 72)
    print("台股活躍股預測程式 v3.1（TWSE 官方 API／僅普通上市股）")
    print("=" * 72)

    print(f"\n[1/4] 抓取近 {LOOKBACK_DAYS} 個有效交易日全市場資料...")
    panel = fetch_panel_data(LOOKBACK_DAYS)
    unique_dates = sorted(panel["date"].unique())
    print(f"  共取得 {len(panel):,} 筆資料，涵蓋日期：{unique_dates[0]} ~ {unique_dates[-1]}")
    print(f"  股票數（去重後）：{panel['stock_id'].nunique():,}")

    print("\n[2/4] 計算活躍度分數...")
    scored = compute_daily_scores(panel)

    print("\n[3/4] 計算趨勢並預測次日活躍度...")
    predicted = predict_next_day_activity(scored)

    if predicted.empty:
        print("  沒有符合最低歷史天數門檻的股票，請增加 LOOKBACK_DAYS 或降低 MIN_HISTORY_DAYS。")
        return

    topn = predicted.head(TOP_N).copy()
    topn.insert(0, "rank", range(1, len(topn) + 1))

    print(f"\n[4/4] 預測次日活躍度前 {TOP_N} 名：")
    print("-" * 72)
    for _, row in topn.iterrows():
        print(
            f"{int(row['rank']):>2}. {row['stock_id']} {row['stock_name']}"
            f"  預測分數={row['predicted_next_score']:.2f}"
            f"  近{RECENT_AVG_DAYS}日均分={row['recent_avg_score']:.2f}"
            f"  最新分數={row['latest_score']:.2f}"
            f"  斜率={row['trend_slope']:+.3f}"
            f"  R2={row['trend_r2']:.3f}"
        )

    display_cols = [
        "rank",
        "stock_id",
        "stock_name",
        "predicted_next_score",
        "recent_avg_score",
        "latest_score",
        "trend_slope",
        "trend_r2",
        "trend_strength",
        "days_used",
        "latest_trading_money",
        "latest_volume",
        "latest_volume_ratio",
        "latest_amplitude",
        "latest_date",
    ]

    full_display_cols = [c for c in display_cols if c != "rank"]

    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        topn[display_cols].to_excel(writer, sheet_name="活躍度前10名", index=False)
        predicted[full_display_cols].to_excel(writer, sheet_name="全市場排名(普通股)", index=False)

        params_df = pd.DataFrame(
            {
                "參數": [
                    "分析日期",
                    "資料來源",
                    "抓取起點",
                    "涵蓋範圍",
                    "回看有效交易日數",
                    "最低歷史天數門檻",
                    "量比基準天數",
                    "最近均分天數",
                    "成交值權重",
                    "量比權重",
                    "成交筆數權重",
                    "振幅權重",
                    "實際使用交易日",
                ],
                "數值": [
                    datetime.date.today().isoformat(),
                    "證交所官方公開 API (twse.com.tw)",
                    f"{data_start_date().isoformat()}（14:00 後含當日）",
                    "僅普通上市股（初步排除 ETF/ETN/權證/特殊商品）",
                    LOOKBACK_DAYS,
                    MIN_HISTORY_DAYS,
                    ROLLING_BASE_DAYS,
                    RECENT_AVG_DAYS,
                    WEIGHT_TRADING_MONEY,
                    WEIGHT_VOLUME_RATIO,
                    WEIGHT_TURNOVER_COUNT,
                    WEIGHT_AMPLITUDE,
                    ", ".join(unique_dates),
                ],
            }
        )
        params_df.to_excel(writer, sheet_name="參數設定", index=False)

    print(f"\n完成！結果已輸出至：{OUTPUT_FILE}")
    print("\n免責聲明：")
    print("1. 本程式為活躍度排序與觀察工具，不是投資報酬預測模型。")
    print("2. 所謂『預測』為基於近期活躍度趨勢的簡化外推，僅供技術參考。")
    print("3. 不構成任何投資建議，請自行判斷並承擔交易風險。")


if __name__ == "__main__":
    main()