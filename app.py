"""
TrendSpider VixFix Webhook Receiver
------------------------------------
Receives POST from TrendSpider when a VixFix + EMA loading zone alert fires.
For each triggered ticker:
  1. Pulls Finviz Elite daily chart
  2. Pulls live stock quote
  3. Fires a Perplexity push notification with all data

Credentials are loaded from environment variables — never hardcoded.
Set these in Railway → Variables:
  FINVIZ_AUTH       = your Finviz Elite auth token
  QUANT_DATA_EMAIL  = your Quant Data login email
  QUANT_DATA_PASS   = your Quant Data password
"""

import os
import json
import subprocess
import requests
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

# All credentials from environment variables
FINVIZ_AUTH      = os.environ.get("FINVIZ_AUTH", "")
QUANT_DATA_EMAIL = os.environ.get("QUANT_DATA_EMAIL", "")
QUANT_DATA_PASS  = os.environ.get("QUANT_DATA_PASS", "")

EMA_LABELS = {
    "50":  "GOOD — 50 EMA",
    "100": "STRONG — 100 EMA",
    "200": "NUCLEAR — 200 EMA",
}


def call_tool(source_id, tool_name, arguments):
    params = json.dumps({
        "source_id": source_id,
        "tool_name": tool_name,
        "arguments": arguments,
    })
    result = subprocess.run(
        ["external-tool", "call", params],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Tool error: {result.stderr}")
    return json.loads(result.stdout)


def get_finviz_chart(ticker):
    url = f"https://elite.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d&auth={FINVIZ_AUTH}"
    path = f"/tmp/{ticker}_vixfix_chart.png"
    resp = requests.get(url, allow_redirects=True, timeout=10)
    if resp.status_code == 200 and resp.content[:4] == b'\x89PNG':
        with open(path, "wb") as f:
            f.write(resp.content)
        return path
    return None


def get_stock_quote(ticker):
    try:
        result = call_tool("finance", "finance_quotes", {
            "ticker_symbols": [ticker],
            "fields": ["price", "change", "changesPercentage", "volume", "avgVolume",
                       "dayLow", "dayHigh"]
        })
        if result and len(result) > 0:
            return result[0]
    except Exception as e:
        print(f"Quote error: {e}")
    return {}


def build_notification_body(ticker, alert_name, quote, ema_level):
    price      = quote.get("price", "N/A")
    change_pct = quote.get("changesPercentage", 0)
    volume     = quote.get("volume", 0)
    avg_vol    = quote.get("avgVolume", 1)
    vol_ratio  = round(volume / avg_vol, 1) if avg_vol else "N/A"
    day_low    = quote.get("dayLow", "N/A")
    day_high   = quote.get("dayHigh", "N/A")
    ema_label  = EMA_LABELS.get(ema_level, f"{ema_level} EMA")
    direction  = "+" if change_pct >= 0 else ""
    vol_flag   = " HIGH VOLUME" if vol_ratio != "N/A" and vol_ratio >= 2 else ""
    now        = datetime.now().strftime("%I:%M %p ET")

    return (
        f"LOADING ZONE — {ema_label}\n\n"
        f"{ticker} | ${price} | {direction}{change_pct:.2f}%\n"
        f"Range: ${day_low} – ${day_high}\n"
        f"Volume: {vol_ratio}x avg{vol_flag}\n\n"
        f"Signal: VixFix outstretched + price above {ema_level} EMA pulling back\n"
        f"Action: Check Level 2 on Bloomberg — buyers stacking = entry confirmed\n\n"
        f"Open Bloomberg: {ticker} OMON for options chain\n"
        f"Time: {now}"
    )


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.get_data(as_text=True)
        print(f"[WEBHOOK] Received: {raw}")

        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            payload = {}

        ticker = (
            payload.get("symbol") or
            payload.get("ticker") or
            request.args.get("symbol") or
            "UNKNOWN"
        ).upper().strip()

        alert_name = (
            payload.get("alert") or
            payload.get("alert_name") or
            request.args.get("alert") or
            "VixFix Loading Zone"
        )

        ema_level = "50"
        for lvl in ["200", "100", "50"]:
            if lvl in alert_name:
                ema_level = lvl
                break

        if ticker == "UNKNOWN":
            return jsonify({"status": "error", "message": "No ticker found"}), 400

        quote      = get_stock_quote(ticker)
        chart_path = get_finviz_chart(ticker)
        body       = build_notification_body(ticker, alert_name, quote, ema_level)
        ema_label  = EMA_LABELS.get(ema_level, f"{ema_level} EMA")
        title      = f"VixFix Loading Zone — {ticker} at {ema_label.split(' — ')[1]}"

        try:
            call_tool("notifications", "send_notification", {
                "title": title,
                "body": body,
                "channels": ["push", "in_app"]
            })
        except Exception as e:
            print(f"[WEBHOOK] Notification error: {e}")

        return jsonify({"status": "ok", "ticker": ticker, "notified": True}), 200

    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running", "service": "TrendSpider VixFix Webhook"}), 200


@app.route("/test/<ticker>", methods=["GET"])
def test_ticker(ticker):
    ticker = ticker.upper()
    quote  = get_stock_quote(ticker)
    body   = build_notification_body(ticker, "TEST_VixFix_200EMA", quote, "200")
    return jsonify({"ticker": ticker, "quote": quote, "notification_body": body}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
