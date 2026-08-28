# -*- coding: utf-8 -*-
"""
台股「活躍股篩選 + 完整分析 + 紙上交易模擬」整合流程 v1.1
==========================================================
沿用 v1.0 的作法：用 importlib 動態載入既有 .py 檔案，不重寫其邏輯。

流程：
    Step 1：活躍股篩選（tw_active_stocks_predictor v2.1）→ TOP_N_ACTIVE 檔
    Step 2：逐檔完整分析（claude_stock_analyzer v3.7）→ 寫入長期記錄 Excel
    Step 3：紙上交易模擬推進一天（tools/paper_trading.py）★ v1.1 新增
    Step 4：回填前一日的實際結果（tools/log_review.py）    ★ v1.1 新增

版本紀錄：
    v1.0 (2026-08-25) 初版，串接 predictor v2.1 + analyzer v3.6
    v1.1 (2026-08-28) analyzer 升級至 v3.7；活躍股篩選改用 v3.1（找不到時
                       自動退回 v3.0 / v2.1）；新增紙上交易模擬與結果回填。
                       analyzer v3.7 的 Excel 欄位結構與 v3.6 不同，會自動
                       另存新檔（預設 stock_analysis_log_v3.7.xlsx），
                       不會覆蓋你既有的 v3.6 紀錄。

⚠ 使用前必讀：
    1. 本程式會自動尋找下列四支程式，放在「與本程式同一個資料夾」或
       「同資料夾下的 tools/ 子目錄」都可以：
           tw_active_stocks_predictor_v3.1.py（找不到時退回 v3.0 / v2.1）
           claude_stock_analyzer_v3.7.py
           paper_trading.py
           log_review.py
       若檔名不同（例如你之後升到 v3.8），請修改下方參數設定區。
       執行前會先檢查這四支程式是否都找得到，缺哪一支會明確告訴你。
    2. 請在「收盤後」執行（15:00 之後）。盤中執行時 analyzer v3.7 會剔除
       未完成K棒、以前一交易日為基準，模擬器則會拿不到當日完整價格。
    3. TOP_N_ACTIVE 建議 5~10 檔，數字太大會拉長執行時間，也提高被 TWSE
       判定為異常流量而暫時限流的風險。
    4. 「活躍」不等於「值得投資」。活躍度高同時代表波動大，這條流程
       把最活躍的股票餵進分析，等於系統性地選到最容易大跌的一批。
       模擬器裡的 active_equal 策略就是拿來檢驗這件事的對照組。
    5. 只能在本機執行（VS Code）。需要能連線 twse.com.tw 與
       query1.finance.yahoo.com。
    6. 本流程為紙上推演，不下真單，不構成投資建議。
"""

import datetime
import importlib.util
import subprocess
import sys
from pathlib import Path

# ============================================================
# 參數設定區
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

def _find(filename: str) -> Path:
    """
    在幾個常見位置尋找檔案，讓「全部放同一個資料夾」和「工具放 tools/ 子目錄」
    兩種擺法都能直接執行，不用改路徑。找不到時回傳同目錄的路徑，
    由呼叫端印出明確的錯誤訊息。
    """
    for candidate in (SCRIPT_DIR / filename,
                      SCRIPT_DIR / "tools" / filename,
                      SCRIPT_DIR.parent / filename,
                      SCRIPT_DIR.parent / "tools" / filename):
        if candidate.exists():
            return candidate
    return SCRIPT_DIR / filename


# 活躍股篩選程式：優先用最新版。v3.1 修正了 v2.1/v3.0 共同的「永遠不抓
# 今天」問題（收盤後執行時，篩選端會落後分析端整整一個交易日），
# 並排除 ETF/ETN/權證/特別股——實測那正是造成分析異常的那幾檔。
# 找不到新版時往下退，不會因為你還沒換版就跑不動。
PREDICTOR_CANDIDATES = [
    "tw_active_stocks_predictor_v3.1.py",
    "tw_active_stocks_predictor_v3.0.py",
    "tw_active_stocks_predictor_v2.1.py",
]


def _find_predictor() -> Path:
    for name in PREDICTOR_CANDIDATES:
        p = _find(name)
        if p.exists():
            return p
    return SCRIPT_DIR / PREDICTOR_CANDIDATES[0]


PREDICTOR_SCRIPT = _find_predictor()
ANALYZER_SCRIPT = _find("claude_stock_analyzer_v3.7.py")
PAPER_TRADING = _find("paper_trading.py")
LOG_REVIEW = _find("log_review.py")

# analyzer v3.7 的紀錄檔：若目錄下已有 v3.6 建立的 stock_analysis_log.xlsx，
# v3.7 會因欄位不一致而自動另存 stock_analysis_log_v3.7.xlsx；
# 若是全新環境則會直接用 stock_analysis_log.xlsx。兩種都要能對上，
# 所以下面 main() 會實際去確認檔名，這裡只是預設值。
ANALYZER_LOG = SCRIPT_DIR / "stock_analysis_log_v3.7.xlsx"
SIM_STATE = SCRIPT_DIR / "paper_trading_state.json"

TOP_N_ACTIVE = 10          # 進入完整分析的活躍股檔數
ANALYZER_PAUSE_SEC = 1.5   # 分析多檔股票時每檔之間的延遲秒數

# 模擬期間的最後一天。到這一天會自動改用 settle（全數出清並印出結算報告）。
SIM_LAST_DAY = datetime.date(2026, 9, 4)   # 下週五


def _load_module(path: Path, module_name: str):
    if not path.exists():
        print(f"✗ 找不到檔案：{path}")
        print(f"  請確認 {module_name} 的路徑設定正確（目前指向：{path}）")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_tool(script: Path, args: list[str], label: str) -> bool:
    """以子程序執行工具，失敗不中斷整條流程。"""
    if not script.exists():
        print(f"  ⚠ 找不到 {script}，略過{label}")
        return False
    cmd = [sys.executable, str(script)] + args
    print(f"  執行：{' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, cwd=SCRIPT_DIR)
        if result.returncode != 0:
            print(f"  ⚠ {label}回傳非零結束碼 {result.returncode}")
            return False
        return True
    except Exception as e:
        print(f"  ⚠ {label}執行失敗：{type(e).__name__}: {e}")
        return False


def main():
    today = datetime.date.today()
    is_last_day = today >= SIM_LAST_DAY

    print("=" * 64)
    print("台股活躍股篩選 + 完整分析 + 紙上交易模擬  整合流程 v1.1")
    print(f"執行日期：{today}" + ("（模擬結算日）" if is_last_day else ""))
    print("=" * 64)

    # 先確認四支程式都找得到，缺什麼一次講清楚，不要跑到一半才失敗
    required = {
        "活躍股篩選程式": PREDICTOR_SCRIPT,
        "個股分析程式": ANALYZER_SCRIPT,
        "紙上交易模擬器": PAPER_TRADING,
        "結果回填工具": LOG_REVIEW,
    }
    missing = {label: p for label, p in required.items() if not p.exists()}
    if missing:
        print("\n✗ 缺少以下程式，無法執行：")
        for label, p in missing.items():
            print(f"    {label}：找不到 {p.name}")
        print(f"\n  請把它們放在下列任一位置：")
        print(f"    {SCRIPT_DIR}")
        print(f"    {SCRIPT_DIR / 'tools'}")
        sys.exit(1)

    print("\n[檢查] 四支程式都已找到：")
    for label, p in required.items():
        try:
            shown = p.relative_to(SCRIPT_DIR)
        except ValueError:
            shown = p
        print(f"    {label}：{shown}")

    now = datetime.datetime.now()
    if datetime.time(9, 0) <= now.time() < datetime.time(14, 0):
        print("\n⚠ 目前是台股盤中時段。analyzer v3.7 會剔除未完成K棒、")
        print("   以前一交易日為基準；模擬器也拿不到當日完整價格。")
        print("   建議 15:00 之後再執行，才能得到當日完整的分析與模擬。\n")

    print(f"\n[載入] 活躍股篩選程式：{PREDICTOR_SCRIPT.name}")
    if "v3.1" not in PREDICTOR_SCRIPT.name:
        print(f"   ⚠ 目前使用的不是 v3.1。v2.1/v3.0 有一個共同問題：抓取起點")
        print(f"     寫死為「昨天」，收盤後執行時篩選端會比分析端落後一個交易日。")
        print(f"     建議把 tw_active_stocks_predictor_v3.1.py 放進本資料夾。")
    predictor = _load_module(PREDICTOR_SCRIPT, "tw_active_stocks_predictor")
    print(f"[載入] 個股分析程式：{ANALYZER_SCRIPT.name}")
    analyzer = _load_module(ANALYZER_SCRIPT, "claude_stock_analyzer")

    # ------------------------------------------------------------
    # Step 1：市場活躍股篩選
    # ------------------------------------------------------------
    print(f"\n{'='*64}")
    print(f"Step 1／4：市場活躍股篩選（近 {predictor.LOOKBACK_DAYS} 個交易日，僅上市股票）")
    print(f"{'='*64}")

    panel = predictor.fetch_panel_data(predictor.LOOKBACK_DAYS)
    scored = predictor.compute_daily_scores(panel)
    predicted = predictor.predict_next_day_activity(scored)
    predicted = predicted.sort_values("predicted_next_score", ascending=False)
    top_active = predicted.head(TOP_N_ACTIVE).reset_index(drop=True)

    print(f"\n活躍度前 {TOP_N_ACTIVE} 名：")
    for i, row in top_active.iterrows():
        print(f"  {i + 1:>2}. {row['stock_id']} {row['stock_name']}"
              f"　預測分數={row['predicted_next_score']:.2f}")

    screen_output = SCRIPT_DIR / f"活躍股篩選結果_{today.isoformat()}.xlsx"
    top_active.to_excel(screen_output, index=False)
    print(f"\n篩選結果已另存：{screen_output}")

    # ------------------------------------------------------------
    # Step 2：逐檔完整分析
    # ------------------------------------------------------------
    tickers = [f"{sid}.TW" for sid in top_active["stock_id"].tolist()]

    print(f"\n{'='*64}")
    print(f"Step 2／4：對前 {len(tickers)} 檔活躍股進行完整分析")
    print(f"{'='*64}")
    print(f"股票清單：{tickers}")
    print(f"分析結果將寫入 analyzer v3.7 的長期記錄 Excel")

    analyzer._run_batch(tickers, pause_sec=ANALYZER_PAUSE_SEC)

    # analyzer v3.7 因欄位變動會自動另存新檔，實際檔名可能帶 _v3.7 後綴
    log_path = ANALYZER_LOG
    if not log_path.exists():
        candidates = sorted(SCRIPT_DIR.glob("stock_analysis_log*.xlsx"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            log_path = candidates[0]
            print(f"\n  （偵測到實際的紀錄檔為：{log_path.name}）")

    # ------------------------------------------------------------
    # Step 3：紙上交易模擬推進一天
    # ------------------------------------------------------------
    print(f"\n{'='*64}")
    print(f"Step 3／4：紙上交易模擬（{today}）")
    print(f"{'='*64}")

    if not SIM_STATE.exists():
        print("  尚未建立模擬，先執行 init（初始資金 100 萬 × 6 個獨立策略帳戶）")
        _run_tool(PAPER_TRADING, ["init", "--state", str(SIM_STATE),
                                  "--capital", "1000000"], "模擬初始化")

    sim_cmd = "settle" if is_last_day else "step"
    _run_tool(PAPER_TRADING, [sim_cmd, "--state", str(SIM_STATE),
                              "--date", today.isoformat(),
                              "--log", str(log_path)], "紙上交易模擬")

    # ------------------------------------------------------------
    # Step 4：回填前一日實際結果
    # ------------------------------------------------------------
    print(f"\n{'='*64}")
    print(f"Step 4／4：回填實際結果並更新驗證統計")
    print(f"{'='*64}")
    _run_tool(LOG_REVIEW, [str(log_path), "--write-back"], "結果回填")

    # ------------------------------------------------------------
    print(f"\n{'='*64}")
    print("整合流程執行完畢")
    print(f"{'='*64}")
    print(f"  活躍股篩選結果：{screen_output.name}")
    print(f"  個股分析紀錄：  {log_path.name}")
    print(f"  模擬狀態：      {SIM_STATE.name}")
    if is_last_day:
        print(f"\n  ★ 今天是模擬結算日，上方已印出完整結算報告。")
        print(f"     若要輸出 Excel 版本：")
        print(f"       python tools/paper_trading.py report --state {SIM_STATE.name} -o 模擬結算報告.xlsx")
    else:
        remaining = (SIM_LAST_DAY - today).days
        print(f"\n  距離模擬結算日（{SIM_LAST_DAY}）還有 {remaining} 天。")
        print(f"     隨時查看目前狀況：")
        print(f"       python tools/paper_trading.py report --state {SIM_STATE.name}")
    print("\n免責聲明：本流程為技術整合與紙上推演，不下真單，")
    print("篩選、分析與模擬結果皆不構成投資建議，請自行判斷並承擔交易風險。")


if __name__ == "__main__":
    main()
