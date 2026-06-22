"""
台股AI分析系統 — TWSE 共用工具
提供：休市判斷、加權指數、自選股報價、除權息行事曆、月營收
"""
import json
import urllib.request
from datetime import datetime, date, timedelta
from config import setup_logging, load_watchlist

logger = setup_logging("twse_utils")

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

STOCK_NAMES = {
    "2312": "金寶",   "2356": "英業達", "2449": "京元電",
    "3665": "貿聯-KY","3706": "神達",   "4958": "臻鼎-KY",
    "6235": "波絡威", "6285": "啟碁",   "6667": "李洋",
    "8033": "雷虎",
}


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def is_trading_day() -> bool:
    """確認今天 TWSE 是否有開盤（抓 FMTQIK 加權指數確認）。"""
    today = datetime.now()
    # 週末直接跳過
    if today.weekday() >= 5:
        return False
    try:
        data = _get("https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK")
        if not data:
            return False
        # 確認最新一筆日期是否為今天
        latest = data[-1].get("Date", "")  # 格式 "1130619"
        if len(latest) == 7:
            y = int(latest[:3]) + 1911
            m = int(latest[3:5])
            d = int(latest[5:7])
            return date(y, m, d) == today.date()
        return False
    except Exception as e:
        logger.debug(f"休市判斷失敗（預設為開盤）: {e}")
        return True  # 抓不到資料時預設繼續執行


def fetch_taiex() -> dict:
    """抓加權指數當日資料，回傳 {index, change, pct, date}。"""
    try:
        data = _get("https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK")
        if not data:
            return {}
        d = data[-1]
        index = float(str(d.get("ClosingIndex", "0")).replace(",", ""))
        prev  = float(str(d.get("OpeningIndex", index)).replace(",", ""))
        chg_str = str(d.get("Change", "0")).replace(",", "").replace("+", "")
        chg = float(chg_str) if chg_str not in ("", "--") else 0.0
        base = index - chg
        pct = chg / base * 100 if base else 0
        return {"index": index, "change": chg, "pct": pct}
    except Exception as e:
        logger.debug(f"加權指數抓取失敗: {e}")
        return {}


def fetch_stock_prices(codes=None) -> dict:
    """抓自選股今日收盤價，回傳 {code: {name, close, change, pct}}。"""
    if codes is None:
        codes = load_watchlist()
    try:
        data = _get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
        stock_map = {d.get("Code", ""): d for d in data}
    except Exception as e:
        logger.debug(f"股價抓取失敗: {e}")
        return {}

    result = {}
    for code in codes:
        d = stock_map.get(code)
        if not d:
            continue
        try:
            close = float(str(d.get("ClosingPrice", "0")).replace(",", ""))
            chg_str = str(d.get("Change", "0")).replace(",", "").replace("+", "")
            chg = float(chg_str) if chg_str not in ("", "--", "X0") else 0.0
            base = close - chg
            pct = chg / base * 100 if base else 0
            vol_str = str(d.get("TradeVolume", "0")).replace(",", "")
            vol = int(float(vol_str)) if vol_str not in ("", "--") else 0
            result[code] = {
                "name": STOCK_NAMES.get(code, code),
                "close": close,
                "change": chg,
                "pct": pct,
                "vol": vol,
            }
        except Exception:
            continue
    return result


def fetch_dividend_calendar(codes=None, days_ahead=7) -> list:
    """
    抓自選股近 N 天內的除權息日期。
    使用 TWSE openapi /v1/exchangeReport/TWT49U
    回傳 [{code, name, date, type, amount}]
    """
    if codes is None:
        codes = load_watchlist()
    results = []
    today = date.today()
    deadline = today + timedelta(days=days_ahead)

    try:
        data = _get("https://openapi.twse.com.tw/v1/exchangeReport/TWT49U")
        for row in data:
            code = str(row.get("Code", "")).strip()
            if code not in codes:
                continue
            # 除息日 ExDividendDate 格式 "1130620"
            raw = str(row.get("ExDividendDate", "")).strip()
            if len(raw) != 7:
                continue
            try:
                ex_date = date(int(raw[:3]) + 1911, int(raw[3:5]), int(raw[5:7]))
            except Exception:
                continue
            if today <= ex_date <= deadline:
                cash = row.get("CashDividend", "")
                stock_div = row.get("StockDividend", "")
                div_type = []
                if cash and cash not in ("", "0", "0.0000"):
                    div_type.append(f"現金{cash}元")
                if stock_div and stock_div not in ("", "0", "0.0000"):
                    div_type.append(f"股票{stock_div}")
                results.append({
                    "code": code,
                    "name": STOCK_NAMES.get(code, code),
                    "date": ex_date,
                    "dividend": "、".join(div_type) if div_type else "見公告",
                })
    except Exception as e:
        logger.debug(f"除權息行事曆抓取失敗: {e}")
    return results


def fetch_monthly_revenue(codes=None) -> list:
    """
    抓自選股最新月營收與 YoY 年增率。
    使用 TWSE /v1/exchangeReport/t163sb05
    回傳 [{code, name, month, revenue, yoy}]
    """
    if codes is None:
        codes = load_watchlist()
    results = []
    try:
        data = _get("https://openapi.twse.com.tw/v1/exchangeReport/t163sb05")
        for row in data:
            code = str(row.get("公司代號", "")).strip()
            if code not in codes:
                continue
            try:
                rev = float(str(row.get("當月營收", "0")).replace(",", ""))
                yoy_str = str(row.get("去年同月增減(%)", "")).replace(",", "").replace("+", "")
                yoy = float(yoy_str) if yoy_str not in ("", "--") else None
                month = str(row.get("出表日期", ""))[:5]  # 民國年月
                results.append({
                    "code": code,
                    "name": STOCK_NAMES.get(code, code),
                    "month": month,
                    "revenue": rev,
                    "yoy": yoy,
                })
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"月營收抓取失敗: {e}")
    return results


def clean_old_logs(log_dir, days=7):
    """刪除 log_dir 內超過 days 天的 .log 檔。"""
    import os
    cutoff = datetime.now().timestamp() - days * 86400
    deleted = 0
    try:
        for f in os.listdir(log_dir):
            if not f.endswith(".log"):
                continue
            fp = os.path.join(log_dir, f)
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
                deleted += 1
        if deleted:
            logger.info(f"✓ 清理舊 log {deleted} 個")
    except Exception as e:
        logger.debug(f"log 清理失敗: {e}")


def fetch_stock_ohlc(code: str, months: int = 4) -> list:
    """
    抓個股近 months 個月的日K資料。
    回傳 list of [date_str, open, high, low, close, vol]（皆為 float/int）。
    """
    now = datetime.now()
    rows = []
    for i in range(months - 1, -1, -1):
        month = now.month - i
        year  = now.year
        while month <= 0:
            month += 12
            year  -= 1
        ds = f"{year}{month:02d}01"
        try:
            url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={ds}&stockNo={code}&response=json"
            data = _get(url)
            if data and data.get("stat") == "OK" and data.get("data"):
                for row in data["data"]:
                    try:
                        close = float(str(row[6]).replace(",", ""))
                        rows.append(close)
                    except Exception:
                        continue
        except Exception:
            continue
    return rows


def fetch_volume_ma(code: str, days: int = 20):
    """
    抓個股近 N 日成交量均值（股數），用於爆量偵測。
    抓最近兩個月的月成交資料，取最後 days 筆平均。
    """
    from datetime import date as _date
    import calendar
    now = datetime.now()
    vols = []
    for i in range(2, -1, -1):  # 抓最近3個月，確保夠 20 筆
        m = datetime(now.year, now.month, 1)
        # 往前推 i 個月
        month = now.month - i
        year  = now.year
        while month <= 0:
            month += 12
            year  -= 1
        ds = f"{year}{month:02d}01"
        try:
            url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={ds}&stockNo={code}&response=json"
            data = _get(url)
            if data and data.get("stat") == "OK" and data.get("data"):
                for row in data["data"]:
                    try:
                        v = int(str(row[1]).replace(",", ""))
                        vols.append(v)
                    except Exception:
                        continue
        except Exception:
            continue
    if len(vols) < 5:
        return None
    return sum(vols[-days:]) / min(len(vols), days)
