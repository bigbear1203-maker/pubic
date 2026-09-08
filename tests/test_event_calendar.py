# -*- coding: utf-8 -*-
"""
事件日曆離線測試
================
完全不連網。驗證的重點是「日期算得對」——事件日期算錯不會噴錯誤，
只會讓後面的統計安靜地算在錯的日子上，那是最難發現的一種 bug。

執行：python tests/test_event_calendar.py
"""

import datetime as dt
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import event_calendar as ec                                     # noqa: E402

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


D = dt.date


# ----------------------------------------------------------------------
def test_calendar_source():
    print("\n[1] 交易日曆來自分析程式，不是自己複製一份")
    holidays, coverage, note = ec.load_trading_calendar()
    check("成功讀到假日清單", not note and len(holidays) > 0, note)
    check("讀到 2026 春節（2/16）", D(2026, 2, 16) in holidays)
    check("讀到 2026 中秋（9/25）", D(2026, 9, 25) in holidays)
    check("讀到涵蓋截止日", coverage == D(2026, 12, 31), str(coverage))

    cal = ec.TradingCalendar()
    check("週六不是交易日", not cal.is_trading_day(D(2026, 9, 12)))
    check("國定假日不是交易日", not cal.is_trading_day(D(2026, 10, 9)))
    check("平常日是交易日", cal.is_trading_day(D(2026, 9, 8)))

    # 讀不到來源時必須明講，而不是安靜地用空清單
    missing = ROOT / "不存在的檔案_zzz.py"
    h2, _, note2 = ec.load_trading_calendar(missing)
    check("來源讀不到時回報原因而非安靜失敗", h2 == set() and bool(note2), note2)
    bad_cal = ec.TradingCalendar(holidays=set(), coverage_until=None, note=note2)
    check("空清單的 TradingCalendar 會帶著提醒", bool(bad_cal.note))


# ----------------------------------------------------------------------
def test_settlement():
    print("\n[2] 台指期結算日 = 第三個星期三（休市則順延）")
    cal = ec.TradingCalendar()

    # 沒有假日干擾的月份，應該就是第三個星期三本人
    for y, m, expect in [(2026, 9, D(2026, 9, 16)),
                         (2026, 10, D(2026, 10, 21)),
                         (2026, 7, D(2026, 7, 15)),
                         (2026, 12, D(2026, 12, 16))]:
        got = ec.futures_settlement(y, m, cal)
        check(f"{y}-{m:02d} 結算日 = {expect}", got == expect, f"得到 {got}")
        check(f"{y}-{m:02d} 結算日確實是星期三", got.weekday() == 2)

    # 2026-02-18 是春節休市，規則是順延不是提前
    got = ec.futures_settlement(2026, 2, cal)
    check("2026-02 第三個週三(2/18)遇春節，順延到 2/23", got == D(2026, 2, 23),
          f"得到 {got}")
    check("順延後仍是交易日", cal.is_trading_day(got))
    check("是順延不是提前（晚於原定日）", got > D(2026, 2, 18))

    # 結算前一交易日必須是交易日，且真的在結算日之前
    for y, m in [(2026, 2), (2026, 9), (2026, 10)]:
        s = ec.futures_settlement(y, m, cal)
        pre = cal.prev_trading_day(s)
        check(f"{y}-{m:02d} 結算前一交易日 {pre} 是交易日且早於結算日",
              cal.is_trading_day(pre) and pre < s)


# ----------------------------------------------------------------------
def test_revenue():
    print("\n[3] 月營收公布截止日 = 每月 10 日（非交易日順延）")
    cal = ec.TradingCalendar()

    check("2026-09-10 是週四，就是當天",
          ec.revenue_deadline(2026, 9, cal) == D(2026, 9, 10))
    # 2026-10-10 是週六，且 10/9 是國慶連假
    got = ec.revenue_deadline(2026, 10, cal)
    check("2026-10-10 是週六，順延到 10/12（週一）", got == D(2026, 10, 12),
          f"得到 {got}")
    check("順延後是交易日", cal.is_trading_day(got))

    for y, m in [(2026, x) for x in range(1, 13)]:
        d = ec.revenue_deadline(y, m, cal)
        check(f"{y}-{m:02d} 截止日 {d} 是交易日且不早於 10 日",
              cal.is_trading_day(d) and d >= D(y, m, 10))


# ----------------------------------------------------------------------
def test_events_between():
    print("\n[4] 區間查詢")
    cal = ec.TradingCalendar()

    sep = ec.events_between(D(2026, 9, 1), D(2026, 9, 30), cal)
    check("2026-09 共 4 個事件日", len(sep) == 4, sorted(sep))
    check("9/16 是結算日", ec.FUTURES_SETTLEMENT in sep.get(D(2026, 9, 16), []))
    check("9/15 是結算前一日", ec.FUTURES_PRE in sep.get(D(2026, 9, 15), []))
    check("9/10 是營收截止日", ec.REVENUE_DEADLINE in sep.get(D(2026, 9, 10), []))
    check("9/11 是營收後首日", ec.REVENUE_NEXT in sep.get(D(2026, 9, 11), []))

    # 窄區間必須跟寬區間對同一天給出一樣的答案。
    # 這條是在守「跨月事件被漏掉」——衍生日可能落在鄰月，只掃區間內
    # 的月份會漏算，而漏算不會有任何錯誤訊息。
    wide = ec.events_between(D(2026, 1, 1), D(2026, 12, 31), cal)
    mismatch = []
    for day, names in wide.items():
        narrow = ec.events_between(day, day, cal).get(day, [])
        if sorted(narrow) != sorted(names):
            mismatch.append(day)
    check(f"單日查詢與全年查詢結果一致（檢查 {len(wide)} 天）",
          not mismatch, str(mismatch[:5]))

    check("全年事件日數量合理（每月 4 個上下）",
          40 <= len(wide) <= 48, str(len(wide)))
    check("沒有任何一天出現重複事件名稱",
          all(len(v) == len(set(v)) for v in wide.values()))
    check("events_on 與 events_between 一致",
          ec.events_on(D(2026, 9, 16), cal) == sep[D(2026, 9, 16)])
    check("非事件日回傳空清單", ec.events_on(D(2026, 9, 8), cal) == [])

    empty = ec.events_between(D(2026, 9, 17), D(2026, 9, 18), cal)
    check("沒有事件的區間回傳空 dict", empty == {}, str(empty))


# ----------------------------------------------------------------------
def test_coverage_warning():
    print("\n[5] 假日清單過期必須出聲")
    cal = ec.TradingCalendar()
    check("涵蓋範圍內不警告", cal.coverage_warning(D(2026, 12, 31)) == "")
    warn = cal.coverage_warning(D(2027, 1, 5))
    check("超出涵蓋範圍會警告", "超出涵蓋範圍" in warn, warn)

    buf = io.StringIO()
    with redirect_stdout(buf):
        ec.print_events(ec.events_between(D(2027, 1, 1), D(2027, 1, 31), cal),
                        cal, today=D(2027, 1, 1))
    check("列印 2027 年事件時畫面上看得到警告", "超出涵蓋範圍" in buf.getvalue())


# ----------------------------------------------------------------------
def test_parsers():
    print("\n[6] 紀錄檔欄位解析")
    check("ISO 字串", ec._to_date("2026-09-10") == D(2026, 9, 10))
    check("斜線格式", ec._to_date("2026/09/10") == D(2026, 9, 10))
    check("含時間", ec._to_date("2026-09-10 15:05:00") == D(2026, 9, 10))
    check("datetime 物件", ec._to_date(dt.datetime(2026, 9, 10, 15, 5)) == D(2026, 9, 10))
    check("date 物件", ec._to_date(D(2026, 9, 10)) == D(2026, 9, 10))
    for bad in (None, "", "   ", "nan", "NaT", "壞掉的日期", "2026-13-45"):
        check(f"無法解析的值回傳 None：{bad!r}", ec._to_date(bad) is None)

    rate, n = ec._hit_rate([True, False, True, True])
    check("布林命中率", rate == 75.0 and n == 4, f"{rate} {n}")
    rate, n = ec._hit_rate(["是", "否", "是", "TRUE", "false"])
    check("中英文字串命中率", abs(rate - 60.0) < 1e-9 and n == 5, f"{rate} {n}")
    rate, n = ec._hit_rate([1, 0, 1])
    check("0/1 命中率", abs(rate - 66.67) < 0.01 and n == 3, f"{rate} {n}")
    rate, n = ec._hit_rate(["", None, "abc"])
    check("全部無法判讀時回傳 None", rate is None and n == 0)


# ----------------------------------------------------------------------
def test_display_width():
    print("\n[7] 中文欄位對齊")
    check("中文字寬度算 2", ec._dw("事件日") == 6)
    check("英數字寬度算 1", ec._dw("abc12") == 5)
    check("混合", ec._dw("事件日x") == 7)
    check("靠左補到指定顯示寬度", ec._dw(ec._pad("事件日", 10)) == 10)
    check("靠右補到指定顯示寬度", ec._dw(ec._pad("一般日", 10, ">")) == 10)
    check("超長不截斷", ec._pad("一二三四五六", 4) == "一二三四五六")


# ----------------------------------------------------------------------
def _write_log(rows, path):
    import pandas as pd
    pd.DataFrame(rows).to_excel(path, index=False)


def _run_stats(rows):
    """回傳 (exit_code, 畫面輸出)。"""
    cal = ec.TradingCalendar()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "stock_analysis_log_test.xlsx"
        _write_log(rows, p)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ec.event_stats(p, cal)
        return code, buf.getvalue()


def test_stats():
    print("\n[8] 事件日 vs 一般日統計")
    cal = ec.TradingCalendar()

    # 缺欄位
    code, out = _run_stats([{"股票代碼": "2330"}])
    check("缺少必要欄位時回報並結束", code == 1 and "缺少" in out, out[:80])

    # 有欄位但全部沒回填
    code, out = _run_stats([{ec.TARGET_DATE_COL: "2026-09-16",
                             ec.RETURN_COL: None}])
    check("沒有任何回填結果時提示先跑 review",
          code == 1 and "review" in out, out[:120])

    # 樣本數不足要明講，不能給出看起來像結論的結論
    rows = [{ec.TARGET_DATE_COL: "2026-09-16", ec.RETURN_COL: 3.0},
            {ec.TARGET_DATE_COL: "2026-09-08", ec.RETURN_COL: 1.0}]
    code, out = _run_stats(rows)
    check("小樣本時回報成功但標明樣本不足",
          code == 0 and "樣本數不足" in out, out[-300:])
    check("小樣本時不給差異結論", "平均絕對報酬比一般日" not in out)

    # 足夠樣本：刻意讓事件日波動是一般日的 3 倍，統計要抓得出來
    rows = []
    ev = ec.events_between(D(2026, 3, 1), D(2026, 9, 30), cal)
    d = D(2026, 3, 1)
    while d <= D(2026, 9, 30):
        if cal.is_trading_day(d):
            big = bool(ev.get(d))
            for k in range(3):
                r = (3.0 if big else 1.0) * (1 if k % 2 else -1)
                rows.append({ec.TARGET_DATE_COL: d.isoformat(),
                             ec.RETURN_COL: r,
                             "是否命中_綜合分數": bool(k % 2)})
        d += dt.timedelta(days=1)
    code, out = _run_stats(rows)
    check("足夠樣本時給出比較結果", code == 0 and "樣本數不足" not in out)
    check("正確抓到事件日波動較大",
          "事件日的平均絕對報酬比一般日高 2.00 個百分點" in out, out[-400:])
    check("結論明講這是波動差異不是方向差異", "不是方向差異" in out)
    check("有分事件類型的細項", ec.FUTURES_SETTLEMENT in out)
    check("命中率有列出", "命中率" in out)

    # 壞資料不能讓整支掛掉
    rows.append({ec.TARGET_DATE_COL: "壞掉", ec.RETURN_COL: 9.9})
    rows.append({ec.TARGET_DATE_COL: "2026-09-16", ec.RETURN_COL: None})
    code, out = _run_stats(rows)
    check("壞掉的日期被排除且有提示",
          code == 0 and "無法解析" in out, out[:400])


# ----------------------------------------------------------------------
def test_stock_py_passthrough():
    print("\n[9] stock.py 參數轉發（--dry-run 曾被吃掉）")
    src = (ROOT / "stock.py").read_text(encoding="utf-8")
    check("dispatcher 不再用 argparse 解析",
          "ap.add_argument(\"command\"" not in src)
    check("events 有接到 dispatcher", 'cmd == "events"' in src)
    check("archive 仍收得到 dry_run", "cmd_archive(dry_run)" in src)
    check("dry_run 從 extra 判斷", 'dry_run = "--dry-run" in extra' in src)

    import importlib.util
    spec = importlib.util.spec_from_file_location("stock_entry", ROOT / "stock.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    calls = []
    m.run = lambda tool, args=(): calls.append((tool, list(args))) or 0
    m.find_log = lambda: ROOT / "dummy_log.xlsx"

    m.main(["merge", "--dry-run"])
    check("merge --dry-run 有轉給下層工具",
          calls and "--dry-run" in calls[-1][1], str(calls[-1:]))
    m.main(["events", "--stats"])
    check("events --stats 有轉給事件日曆",
          calls[-1][0] == "event_calendar.py" and "--stats" in calls[-1][1],
          str(calls[-1:]))
    m.main(["repair", "--check"])
    check("repair --check 有轉下去", "--check" in calls[-1][1], str(calls[-1:]))

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = m.main(["--dry-run"])
    check("只給 flag 沒給指令時報錯而不是安靜顯示總覽",
          code == 1 and "缺少指令" in buf.getvalue(), buf.getvalue()[:120])


# ----------------------------------------------------------------------
def main():
    print("=" * 64)
    print("  事件日曆離線測試")
    print("=" * 64)
    test_calendar_source()
    test_settlement()
    test_revenue()
    test_events_between()
    test_coverage_warning()
    test_parsers()
    test_display_width()
    test_stats()
    test_stock_py_passthrough()
    print("\n" + "=" * 64)
    print(f"  通過 {_passed} 項 / 失敗 {_failed} 項")
    print("=" * 64)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
