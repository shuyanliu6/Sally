import logging

import pandas as pd
import requests

from quantamental.config.settings import (
    STOP_LOSS_ALERT_PCT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)

logger = logging.getLogger(__name__)


def check_stops(
    positions_df: pd.DataFrame,
    latest_prices: dict[str, float],
) -> list[dict]:
    """Return a list of alert dicts for positions within STOP_LOSS_ALERT_PCT of stop.

    Each alert dict has: symbol, current_price, stop_loss_price, distance_pct
    """
    alerts = []
    if positions_df.empty:
        return alerts

    for _, row in positions_df.iterrows():
        symbol = row["symbol"]
        stop = row.get("stop_loss_price")
        if not stop or pd.isna(stop):
            continue

        current = latest_prices.get(symbol)
        if current is None:
            continue

        # Distance as % above stop (positive = safe, negative = breached)
        distance_pct = (current - stop) / stop

        if distance_pct <= STOP_LOSS_ALERT_PCT:
            alert = {
                "symbol": symbol,
                "current_price": current,
                "stop_loss_price": stop,
                "distance_pct": distance_pct,
            }
            alerts.append(alert)
            logger.warning(
                "STOP ALERT %s: price=%.2f stop=%.2f dist=%.1f%%",
                symbol, current, stop, distance_pct * 100,
            )

    return alerts


def send_telegram_alert(message: str) -> bool:
    """Send a Telegram message. Returns True on success. No-ops if token not set."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured, skipping alert")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram alert sent")
        return True
    except Exception as exc:
        logger.error("Telegram alert failed: %s", exc)
        return False


def format_stop_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return "No stop-loss alerts."
    lines = ["⚠️ STOP-LOSS ALERTS"]
    for a in alerts:
        status = "BREACHED" if a["distance_pct"] <= 0 else f"{a['distance_pct']*100:.1f}% above"
        lines.append(
            f"  {a['symbol']}: ${a['current_price']:.2f} | stop ${a['stop_loss_price']:.2f} | {status}"
        )
    return "\n".join(lines)
