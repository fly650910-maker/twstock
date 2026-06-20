"""
台股AI分析系統 — 網頁版推播模組
推播 台股分析.html 互動網頁到 Telegram。
caption 含：加權指數、美股三大指數、自選股漲跌、持倉損益。
週五額外附上本週績效週報。
推播失敗自動重試最多 3 次。

單獨測試：python3 webapp_push.py
"""
import time
import requests
from datetime import datetime, date, timedelta
from pathlib import Path
from config import BASE_DIR, load_config, load_watchlist, setup_logging
from twse_utils import (
    fetch_taiex, fetch_stock_prices, fetch_dividend_calendar,
    fetch_monthly_revenue, clean_old_logs, is_trading_day
)

logger = setup_logging("webapp_push")
WEBAPP_FILE = BASE_DIR / "台股分析.html"
LOG_DIR = BASE_DIR / "logs"

US_INDICES = {
    "^GSPC": "S&P500",
    "^NDX":  "那斯達克",
    "^SOX":  "費半",
}

YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
    "Referer": "https://finance.yahoo.com/",
}


def _fetch_us_summary() -> str:
    parts = []
    for sym, name in US_INDICES.items():
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=5d"
            r = requests.get(url, headers=YF_HEADERS, timeout=8)
            j = r.json()
            meta = j["chart"]["result"][0]["meta"]
            closes = [c for c in j["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
            price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
            prev  = closes[-2] if len(closes) >= 2 else meta.get("chartPreviousClose")
            if not price or not prev:
                continue
            pct = (price - prev) / prev * 100
            parts.append(f"{name} {'▲' if pct>=0 else '▼'}{abs(pct):.1f}%")
        except Exception as e:
            logger.debug(f"美股 {sym} 失敗: {e}")
    return "  ".join(parts)


def _build_holding_pnl(prices: dict) -> str:
    """計算持倉損益，從 config 讀取成本。"""
    cfg = load_config()
    holdings = cfg.get("holdings", {})
    if not holdings:
        return ""
    total_cost = 0
    total_pnl = 0
    lines = []
    for code, hd in holdings.items():
        cost = hd.get("cost", 0)
        shares = hd.get("shares", 0)
        if not cost or not shares:
            continue
        p = prices.get(code)
        if not p:
            continue
        pnl = (p["close"] - cost) * shares
        pct = (p["close"] - cost) / cost * 100
        total_cost += cost * shares
        total_pnl += pnl
        arrow = "▲" if pnl >= 0 else "▼"
        lines.append(f"  {p['name']} {arrow}${abs(pnl):,.0f}（{abs(pct):.1f}%）")
    if not lines:
        return ""
    total_pct = total_pnl / total_cost * 100 if total_cost else 0
    arrow = "▲" if total_pnl >= 0 else "▼"
    header = f"💼 持倉損益 {arrow}${abs(total_pnl):,.0f}（{abs(total_pct):.1f}%）"
    return header + "\n" + "\n".join(lines)


def _build_weekly_report(codes) -> str:
    """週五：抓本週五交易日收盤價，計算週漲跌幅排行。"""
    today = date.today()
    if today.weekday() != 4:  # 只有週五
        return ""
    try:
        import urllib.request, json as _json
        results = []
        # 抓本週一到今天（最多5天）的收盤價
        from twse_utils import HEADERS
        # 用 STOCK_DAY_ALL 只能拿到最近一日，改抓個股月資料取本週
        # 簡化版：從 STOCK_DAY_ALL 拿今日收盤，與5日前比較
        day_all = _json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                    headers=HEADERS
                ), timeout=12
            ).read()
        )
        stock_map = {d.get("Code", ""): d for d in day_all}

        for code in codes:
            d = stock_map.get(code)
            if not d:
                continue
            try:
                close = float(str(d.get("ClosingPrice", "0")).replace(",", ""))
                # 用開盤價估算週初（簡化，實際可擴充為抓5日歷史）
                open_price = float(str(d.get("OpeningPrice", close)).replace(",", ""))
                # 改從 Change 欄位累積較準，這裡先用當週累計欄
                week_chg = float(str(d.get("Change", "0")).replace(",", "").replace("+", "") or "0")
                from twse_utils import STOCK_NAMES
                name = STOCK_NAMES.get(code, code)
                results.append((name, code, week_chg, close))
            except Exception:
                continue

        if not results:
            return ""
        results.sort(key=lambda x: x[2], reverse=True)
        lines = []
        for name, code, chg, close in results:
            arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "－")
            lines.append(f"  {name} {arrow}{abs(chg):.2f}元 收{close}")
        return "📅 本週收盤排行\n" + "\n".join(lines)
    except Exception as e:
        logger.debug(f"週報失敗: {e}")
        return ""


def _build_today_events(codes) -> str:
    """早盤：抓今日及未來3天的除權息事件。"""
    try:
        events = fetch_dividend_calendar(codes, days_ahead=3)
        if not events:
            return ""
        today = date.today()
        lines = []
        for e in events:
            label = "今日" if e["date"] == today else f"{e['date'].strftime('%m/%d')}"
            lines.append(f"  {label} {e['name']}（{e['code']}）除權息 {e['dividend']}")
        return "📋 近期除權息\n" + "\n".join(lines)
    except Exception as e:
        logger.debug(f"今日事件抓取失敗: {e}")
        return ""


def _send_with_retry(token, chat_id, caption, filepath, max_retry=3) -> bool:
    for attempt in range(1, max_retry + 1):
        try:
            with open(filepath, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"document": (Path(filepath).name, f, "text/html")},
                    timeout=60,
                )
            if r.ok and r.json().get("ok"):
                return True
            logger.warning(f"推播失敗（第{attempt}次）: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"推播異常（第{attempt}次）: {e}")
        if attempt < max_retry:
            time.sleep(3 * attempt)
    return False


def push_alert(message: str) -> bool:
    """推播純文字警示訊息。"""
    cfg = load_config()
    token    = cfg.get("telegram_bot_token", "")
    chat_ids = [str(x) for x in cfg.get("telegram_chat_ids", [])]
    if not token or not chat_ids:
        return False
    ok = True
    for chat_id in chat_ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message},
                timeout=15,
            )
            if not (r.ok and r.json().get("ok")):
                ok = False
        except Exception as e:
            logger.warning(f"文字推播失敗: {e}")
            ok = False
    return ok


def push_webapp(session: str = "收盤分析") -> bool:
    # 清理舊 log
    clean_old_logs(LOG_DIR, days=7)

    cfg = load_config()
    token    = cfg.get("telegram_bot_token", "")
    chat_ids = [str(x) for x in cfg.get("telegram_chat_ids", [])]
    if not token or not chat_ids:
        logger.warning("Telegram 未設定，跳過推播")
        return False
    if not WEBAPP_FILE.exists():
        logger.warning(f"找不到 {WEBAPP_FILE}")
        return False

    codes = []
    try:
        codes = load_watchlist()
    except Exception:
        pass

    # 抓各類資料
    taiex    = fetch_taiex()
    prices   = fetch_stock_prices(codes)
    us_line  = _fetch_us_summary()
    pnl_str  = _build_holding_pnl(prices)
    weekly   = _build_weekly_report(codes)
    # 今日重點事件（除權息）— 只在早盤推播時附上
    is_morning = "早盤" in session
    today_events = _build_today_events(codes) if is_morning else ""

    # 組合 caption
    now = datetime.now().strftime("%m/%d %H:%M")
    caption = f"📊 {session}｜{now}"

    # 加權指數
    if taiex:
        arrow = "▲" if taiex["change"] >= 0 else "▼"
        caption += f"\n🇹🇼 加權 {arrow}{abs(taiex['pct']):.2f}%（{taiex['index']:,.0f}）"

    # 美股指數
    if us_line:
        caption += f"\n🇺🇸 {us_line}"

    # 自選股
    if prices:
        lines = []
        for code in codes:
            p = prices.get(code)
            if not p:
                continue
            arrow = "▲" if p["change"] > 0 else ("▼" if p["change"] < 0 else "－")
            lines.append(f"  {p['name']} {arrow}{abs(p['pct']):.1f}%（{p['close']}）")
        if lines:
            caption += "\n\n📈 自選股\n" + "\n".join(lines)

    # 今日重點事件（早盤）
    if today_events:
        caption += f"\n\n{today_events}"

    # 持倉損益
    if pnl_str:
        caption += f"\n\n{pnl_str}"

    # 週報（週五）
    if weekly:
        caption += f"\n\n{weekly}"

    caption += "\n\n🌐 https://fly650910-maker.github.io/twstock/台股分析.html\n點連結或下載檔案後用瀏覽器開啟"

    ok = True
    for chat_id in chat_ids:
        success = _send_with_retry(token, chat_id, caption, WEBAPP_FILE)
        if success:
            logger.info(f"✓ 推播成功 → {chat_id}")
        else:
            logger.error(f"✗ 推播失敗（已重試3次）→ {chat_id}")
            ok = False
    return ok


if __name__ == "__main__":
    print("發送測試中…")
    result = push_webapp("手動測試")
    print("✓ 成功，請到 Telegram 查看" if result else "✗ 失敗")
