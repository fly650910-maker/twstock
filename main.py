#!/usr/bin/env python3
"""
台股AI分析系統 — 主程式
排程觸發後推播 台股分析.html 互動網頁到 Telegram。

推播時間可從 ~/.twstock_ai_config.json 的 push_times 欄位設定：
  "push_times": {
    "morning": "08:45",   <- 早盤摘要（預設 08:45）
    "midday":  "12:00",   <- 午盤快照（預設 12:00，留空表示不推）
    "close":   "14:05"    <- 收盤報告（預設 14:05）
  }

launchd plist 只需固定每 30 分鐘或整點執行一次，
main.py 自行判斷當下時間是否對應推播視窗（±10 分鐘容忍）。
"""
import argparse
from datetime import datetime, time
from config import setup_logging, load_config

logger = setup_logging("main")

# 預設推播時間
DEFAULT_TIMES = {
    "morning": "08:45",
    "midday":  "",       # 留空=不推
    "close":   "14:05",
}


def _parse_hm(s: str):
    """將 'HH:MM' 字串解析為 (hour, minute) tuple，失敗回傳 None。"""
    try:
        parts = s.strip().split(":")
        return int(parts[0]), int(parts[1])
    except Exception:
        return None


def _in_window(target_hm, now: datetime, margin_min=10) -> bool:
    """判斷 now 是否在 target_hm 的 ±margin_min 視窗內。"""
    if not target_hm:
        return False
    th, tm = target_hm
    target_mins = th * 60 + tm
    now_mins = now.hour * 60 + now.minute
    return abs(now_mins - target_mins) <= margin_min


def detect_session(now: datetime = None) -> str | None:
    """
    根據當下時間與 config 的 push_times 自動偵測應推播的 session 名稱。
    若不在任何視窗內回傳 None（表示不需推播）。
    """
    if now is None:
        now = datetime.now()
    cfg = load_config()
    pt = {**DEFAULT_TIMES, **cfg.get("push_times", {})}

    if pt.get("morning") and _in_window(_parse_hm(pt["morning"]), now):
        return "早盤分析"
    if pt.get("midday") and _in_window(_parse_hm(pt["midday"]), now):
        return "午盤快照"
    if pt.get("close") and _in_window(_parse_hm(pt["close"]), now):
        return "收盤分析"
    return None


def run_analysis(session: str = "收盤分析"):
    logger.info(f"台股分析系統啟動 — {session}")
    try:
        from webapp_push import push_webapp
        ok = push_webapp(session)
        if ok:
            logger.info("✓ 推播完成")
        else:
            logger.warning("推播失敗，請確認 Telegram 設定")
    except Exception as e:
        logger.error(f"推播失敗: {e}")


def main():
    parser = argparse.ArgumentParser(description="台股AI分析系統")
    parser.add_argument("--session", default="", help="強制指定 session 名稱（跳過自動偵測）")
    parser.add_argument("--morning", action="store_true", help="強制早盤模式")
    parser.add_argument("--midday",  action="store_true", help="強制午盤模式")
    parser.add_argument("--evening", action="store_true", help="強制收盤模式")
    parser.add_argument("--auto",    action="store_true", help="自動依時間偵測 session（用於定時排程）")
    args = parser.parse_args()

    if args.morning:
        session = "早盤分析"
    elif args.midday:
        session = "午盤快照"
    elif args.evening:
        session = "收盤分析"
    elif args.auto or not args.session:
        # 自動偵測：讀取 config push_times，判斷當下屬於哪個視窗
        session = detect_session()
        if session is None:
            now_str = datetime.now().strftime("%H:%M")
            logger.info(f"當前時間 {now_str} 不在任何推播視窗內，跳過")
            return
    else:
        session = args.session

    run_analysis(session)


if __name__ == "__main__":
    main()
