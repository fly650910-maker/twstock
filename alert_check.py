#!/usr/bin/env python3
"""
台股AI分析系統 — 自選股異動警示
功能：
  - 漲跌超過 alert_threshold（從 config 讀取，預設 3%）時推播
  - 到價提醒：比對 config price_targets，當日首次觸達即推播
  - 去重：同一股票同類警示每日只推播一次（logs/alert_sent_YYYYMMDD.json）
  - 只在盤中 09:00-13:35 且確認為交易日才執行

單獨測試：python3 alert_check.py
"""
import json
from datetime import datetime, date
from pathlib import Path
from config import BASE_DIR, setup_logging, load_config, load_watchlist
from twse_utils import fetch_stock_prices, fetch_volume_ma, is_trading_day

logger = setup_logging("alert_check")

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── 去重狀態 ──────────────────────────────────────────
def _dedup_file() -> Path:
    return LOG_DIR / f"alert_sent_{date.today().strftime('%Y%m%d')}.json"


def _load_sent() -> set:
    f = _dedup_file()
    if f.exists():
        try:
            return set(json.loads(f.read_text()))
        except Exception:
            return set()
    return set()


def _mark_sent(keys: list[str]):
    sent = _load_sent()
    sent.update(keys)
    _dedup_file().write_text(json.dumps(list(sent)))


# ── 主邏輯 ────────────────────────────────────────────
def check_alerts():
    now = datetime.now()

    # 交易時間 09:00-13:35（先快速時間判斷，再呼叫 API 確認休市）
    if now.weekday() >= 5:
        logger.info("週末，跳過")
        return
    if not (9 <= now.hour < 13 or (now.hour == 13 and now.minute <= 35)):
        logger.info(f"非盤中（{now.strftime('%H:%M')}），跳過")
        return
    if not is_trading_day():
        logger.info("今日休市，跳過")
        return

    cfg = load_config()
    threshold   = float(cfg.get("alert_threshold", 3.0))
    vol_mult    = float(cfg.get("volume_spike_multiplier", 3.0))
    price_targets: dict = cfg.get("price_targets", {})

    codes = load_watchlist()
    prices = fetch_stock_prices(codes)
    if not prices:
        logger.warning("無法取得報價")
        return

    sent = _load_sent()
    new_keys = []
    alerts = []

    for code in codes:
        p = prices.get(code)
        if not p:
            continue
        name  = p["name"]
        close = p["close"]
        pct   = p["pct"]
        vol   = p.get("vol", 0)

        # ① 漲跌幅警示
        if abs(pct) >= threshold:
            key = f"pct_{code}_{date.today()}"
            if key not in sent:
                arrow = "🔴▲" if pct > 0 else "🟢▼"
                alerts.append(f"{arrow} {name}（{code}）{abs(pct):.1f}%  收{close}元")
                new_keys.append(key)

        # ② 到價提醒
        target = price_targets.get(code)
        if target:
            target = float(target)
            hit_high = close >= target
            hit_low  = close <= target
            if hit_high or hit_low:
                direction = "觸及目標價" if hit_high else "跌破目標價"
                key = f"target_{code}_{direction}_{date.today()}"
                if key not in sent:
                    emoji = "🎯" if hit_high else "⚠️"
                    alerts.append(f"{emoji} {name}（{code}）{direction} {target}元，現價{close}元")
                    new_keys.append(key)

        # ③ 爆量警示（成交量超過 20 日均量 N 倍）
        if vol > 0:
            key = f"vol_{code}_{date.today()}"
            if key not in sent:
                try:
                    vol_ma = fetch_volume_ma(code, 20)
                    if vol_ma and vol >= vol_ma * vol_mult:
                        ratio = vol / vol_ma
                        alerts.append(f"📢 {name}（{code}）爆量！成交量 {vol//1000:,}張，均量 {vol_ma//1000:,.0f}張（{ratio:.1f}倍）")
                        new_keys.append(key)
                except Exception:
                    pass

    if not alerts:
        logger.info(f"無需推播（threshold={threshold}%，去重後0筆）")
        return

    msg = f"⚠️ 自選股警示 {now.strftime('%H:%M')}\n\n" + "\n".join(alerts)
    logger.info(f"推播警示 {len(alerts)} 筆")

    try:
        from webapp_push import push_alert
        if push_alert(msg):
            _mark_sent(new_keys)
        else:
            logger.error("推播失敗，不標記已送出（下次重試）")
    except Exception as e:
        logger.error(f"推播異常: {e}")


if __name__ == "__main__":
    check_alerts()
