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
    """計算持倉損益，優先從 config holdings，fallback 到程式碼預設。"""
    cfg = load_config()
    holdings = cfg.get("holdings", {})
    # fallback：直接寫死的持倉（與 HTML 同步）
    if not holdings:
        holdings = {
            "2312": {"cost": 36.55,  "shares": 3000},
            "2449": {"cost": 282.91, "shares": 2000},
            "3706": {"cost": 94.73,  "shares": 4000},
            "6285": {"cost": 289.7,  "shares": 2000},
            "6667": {"cost": 272.09, "shares": 2000},
            "8033": {"cost": 144.55, "shares": 5000},
        }
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
        lines.append(f"  {p['name']}({code}) {arrow}${abs(pnl):,.0f}（成本{cost} 現{p['close']} {'+' if pct>=0 else ''}{pct:.1f}%）")
    if not lines:
        return ""
    total_pct = total_pnl / total_cost * 100 if total_cost else 0
    arrow = "▲" if total_pnl >= 0 else "▼"
    header = f"💼 持倉總損益 {arrow}${abs(total_pnl):,.0f}（{'+' if total_pct>=0 else ''}{total_pct:.1f}%）"
    return header + "\n" + "\n".join(lines)


def _build_weekly_report(prices: dict) -> str:
    """週五：顯示持倉本週績效週報。"""
    today = date.today()
    if today.weekday() != 4:  # 只有週五
        return ""
    cfg = load_config()
    holdings = cfg.get("holdings", {})
    if not holdings:
        holdings = {
            "2312": {"cost": 36.55,  "shares": 3000},
            "2449": {"cost": 282.91, "shares": 2000},
            "3706": {"cost": 94.73,  "shares": 4000},
            "6285": {"cost": 289.7,  "shares": 2000},
            "6667": {"cost": 272.09, "shares": 2000},
            "8033": {"cost": 144.55, "shares": 5000},
        }
    lines = []
    total_pnl = 0
    total_cost = 0
    for code, hd in holdings.items():
        cost = hd.get("cost", 0)
        shares = hd.get("shares", 0)
        p = prices.get(code)
        if not p or not cost or not shares:
            continue
        pnl = (p["close"] - cost) * shares
        pct = (p["close"] - cost) / cost * 100
        total_pnl += pnl
        total_cost += cost * shares
        arrow = "▲" if pnl >= 0 else "▼"
        lines.append(f"  {p['name']}({code}) {arrow}{'+' if pct>=0 else ''}{pct:.1f}% 收{p['close']}")
    if not lines:
        return ""
    total_pct = total_pnl / total_cost * 100 if total_cost else 0
    arrow = "▲" if total_pnl >= 0 else "▼"
    header = f"📅 本週持倉績效 {arrow}${abs(total_pnl):,.0f}（{'+' if total_pct>=0 else ''}{total_pct:.1f}%）"
    return header + "\n" + "\n".join(lines)


def check_stoploss_alerts(prices: dict) -> list:
    """檢查持倉是否觸發停損停利，回傳警示訊息清單。"""
    cfg = load_config()
    holdings = cfg.get("holdings", {})
    stoploss = cfg.get("stoploss", {})  # {"2312": {"stop": 32, "target": 48}, ...}
    if not stoploss:
        return []
    alerts = []
    for code, sl in stoploss.items():
        p = prices.get(code)
        if not p:
            continue
        price = p["close"]
        name = p["name"]
        stop = sl.get("stop")
        target = sl.get("target")
        if stop and price <= float(stop):
            alerts.append(f"⚠️ 停損警示｜{name}({code}) 現價{price} ≤ 停損{stop}")
        elif target and price >= float(target):
            alerts.append(f"🎯 停利達標｜{name}({code}) 現價{price} ≥ 停利{target}")
    return alerts


def check_target_alerts(prices: dict) -> list:
    """檢查自選股是否觸達到價目標，回傳警示清單。"""
    cfg = load_config()
    targets = cfg.get("targets", {})  # {"2330": 1000, "8033": 180}
    if not targets:
        return []
    alerts = []
    for code, target in targets.items():
        p = prices.get(code)
        if not p:
            continue
        price = p["close"]
        name = p["name"]
        try:
            t = float(target)
        except Exception:
            continue
        if price >= t:
            alerts.append(f"🎯 到價提醒｜{name}({code}) 現價{price} ≥ 目標{t}")
    return alerts


def check_technical_alerts(codes: list, prices: dict) -> list:
    """收盤技術面警示：KD死叉、MACD翻空、跌破季線（60MA）。"""
    from twse_utils import fetch_stock_ohlc
    alerts = []

    def ema(s, n):
        e = s[0]; k = 2 / (n + 1)
        for v in s[1:]: e = v * k + e * (1 - k)
        return e

    def kd9(cls):
        highs = [max(cls[i-8:i+1]) for i in range(8, len(cls))]
        lows  = [min(cls[i-8:i+1]) for i in range(8, len(cls))]
        K, D = 50.0, 50.0
        for i in range(len(highs)):
            r = (cls[i+8]-lows[i]) / (highs[i]-lows[i]+1e-9) * 100
            K = K * 2/3 + r / 3; D = D * 2/3 + K / 3
        return K, D

    for code in codes:
        p = prices.get(code)
        if not p:
            continue
        try:
            closes = fetch_stock_ohlc(code, months=4)
            if not closes or len(closes) < 30:
                continue

            price = closes[-1]

            # 季線（60MA）—需60筆
            if len(closes) >= 61:
                ma60      = sum(closes[-60:]) / 60
                ma60_prev = sum(closes[-61:-1]) / 60
                if price < ma60 and closes[-2] >= ma60_prev:
                    alerts.append(f"📉 跌破季線｜{p['name']}({code}) 現{price:.2f} 季{ma60:.2f}")

            # KD死叉（需17筆以上）
            if len(closes) >= 17:
                K,  D  = kd9(closes)
                Kp, Dp = kd9(closes[:-1])
                if K < D and Kp >= Dp and K < 50:
                    alerts.append(f"📉 KD死叉｜{p['name']}({code}) K{K:.0f} D{D:.0f}")

            # MACD翻空（需27筆）
            if len(closes) >= 27:
                macd_now  = ema(closes[-26:], 12) - ema(closes[-26:], 26)
                macd_prev = ema(closes[-27:-1], 12) - ema(closes[-27:-1], 26)
                if macd_now < 0 and macd_prev >= 0:
                    alerts.append(f"📉 MACD翻空｜{p['name']}({code})")

        except Exception as e:
            logger.debug(f"技術警示 {code} 失敗: {e}")
    return alerts


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
    weekly   = _build_weekly_report(prices)   # 週五才有內容
    # 今日重點事件（除權息）— 只在早盤推播時附上
    is_morning = "早盤" in session
    is_close   = "收盤" in session
    today_events = _build_today_events(codes) if is_morning else ""

    # 停損停利警示、到價提醒、技術面警示（收盤時檢查）
    sl_alerts = check_stoploss_alerts(prices) if is_close else []
    target_alerts = check_target_alerts(prices) if is_close else []
    tech_alerts = check_technical_alerts(codes, prices) if is_close else []

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

    # 停損停利 + 到價提醒
    all_price_alerts = sl_alerts + target_alerts
    if all_price_alerts:
        caption += "\n\n🚨 價格警示\n" + "\n".join(f"  {a}" for a in all_price_alerts)

    # 技術面警示
    if tech_alerts:
        caption += "\n\n📊 技術警示\n" + "\n".join(f"  {a}" for a in tech_alerts)

    # 週報（週五收盤）
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

    # 若有停損停利警示，額外再發一則純文字訊息（讓手機通知更顯眼）
    if sl_alerts and ok:
        alert_msg = "🚨 台股警示\n" + "\n".join(sl_alerts)
        push_alert(alert_msg)

    return ok


if __name__ == "__main__":
    print("發送測試中…")
    result = push_webapp("手動測試")
    print("✓ 成功，請到 Telegram 查看" if result else "✗ 失敗")
