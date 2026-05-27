"""
TrendSpider VixFix Webhook + Schwab Market Data
-------------------------------------------------
Endpoints:
  POST /webhook            — TrendSpider VixFix alert receiver
  GET  /health             — health check
  GET  /test/<ticker>      — test notification
  GET  /schwab/auth        — initiate Schwab OAuth flow
  GET  /schwab/debug       — OAuth callback / token capture
  GET  /schwab/status      — token status check
  GET  /schwab/quotes      — live quotes: ?symbols=HTZ,FOXA,WMT,GME
  GET  /schwab/level2      — Level 2 order book: ?symbol=HTZ
  GET  /schwab/keepalive   — silent token refresh (called by daily cron)

Railway environment variables required:
  FINVIZ_AUTH        — Finviz Elite auth token
  SCHWAB_CLIENT_ID   — Schwab app key
  SCHWAB_CLIENT_SECRET — Schwab app secret
  SCHWAB_CALLBACK_URL  — must match Schwab developer portal exactly
"""

import os
import json
import time
import base64
import subprocess
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect

app = Flask(__name__)

# ── CREDENTIALS ────────────────────────────────────────────────────────────────
FINVIZ_AUTH           = os.environ.get("FINVIZ_AUTH", "bd60c09b-06cb-42ab-9ef7-5b9d7259aedd")
SCHWAB_CLIENT_ID      = os.environ.get("SCHWAB_CLIENT_ID", "JmibNjVXEBxV0ALDHbDzuah9afosZ8YaBTjWM2TAjzuXNyZA")
SCHWAB_CLIENT_SECRET  = os.environ.get("SCHWAB_CLIENT_SECRET", "ylrPEvW7JmHLvrBpcDlX6MAztHg3EikJubvjbfIgUJODRXfAbBupZK2rEDwrAhKX")
SCHWAB_CALLBACK_URL   = os.environ.get("SCHWAB_CALLBACK_URL", "https://web-production-76c25d.up.railway.app/schwab/debug")

SCHWAB_AUTH_URL    = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_TOKEN_URL   = "https://api.schwabapi.com/v1/oauth/token"
SCHWAB_MARKET_URL  = "https://api.schwabapi.com/marketdata/v1"

# Token stored in memory (Railway persists env vars; token refreshed in-process)
_token_store = {}

TOKEN_FILE = "/tmp/schwab_token.json"

EMA_LABELS = {
    "50":  "GOOD — 50 EMA",
    "100": "STRONG — 100 EMA",
    "200": "NUCLEAR — 200 EMA",
}


# ── TOKEN MANAGEMENT ───────────────────────────────────────────────────────────

def _save_token(token_data: dict):
    _token_store.update(token_data)
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f)


def _load_token() -> dict:
    if _token_store:
        return _token_store
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            data = json.load(f)
            _token_store.update(data)
            return data
    return {}


def _refresh_access_token(token_data: dict) -> dict:
    """Exchange refresh token for new access token."""
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh token available")

    credentials = base64.b64encode(
        f"{SCHWAB_CLIENT_ID}:{SCHWAB_CLIENT_SECRET}".encode()
    ).decode()

    resp = requests.post(
        SCHWAB_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    new_token = resp.json()
    new_token["obtained_at"] = time.time()
    # Preserve refresh token if not returned
    if "refresh_token" not in new_token:
        new_token["refresh_token"] = refresh_token
    _save_token(new_token)
    return new_token


def get_valid_token() -> str:
    """Returns a valid access token, refreshing if needed."""
    token = _load_token()
    if not token:
        raise RuntimeError("No token available — re-authorize at /schwab/auth")

    obtained_at = token.get("obtained_at", 0)
    expires_in  = token.get("expires_in", 1800)  # default 30 min
    # Refresh if within 5 minutes of expiry
    if time.time() > obtained_at + expires_in - 300:
        token = _refresh_access_token(token)

    return token["access_token"]


# ── SCHWAB AUTH ROUTES ─────────────────────────────────────────────────────────

@app.route("/schwab/auth")
def schwab_auth():
    """Redirect to Schwab OAuth login."""
    auth_url = (
        f"{SCHWAB_AUTH_URL}"
        f"?response_type=code"
        f"&client_id={SCHWAB_CLIENT_ID}"
        f"&redirect_uri={SCHWAB_CALLBACK_URL}"
    )
    return redirect(auth_url)


@app.route("/schwab/debug")
def schwab_debug():
    """OAuth callback — exchanges code for token."""
    code = request.args.get("code")
    if not code:
        return jsonify({"message": "No code received", "params": dict(request.args)}), 400

    credentials = base64.b64encode(
        f"{SCHWAB_CLIENT_ID}:{SCHWAB_CLIENT_SECRET}".encode()
    ).decode()

    resp = requests.post(
        SCHWAB_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SCHWAB_CALLBACK_URL,
        },
        timeout=15,
    )

    if resp.status_code != 200:
        return f"<pre>Token exchange failed: {resp.status_code}\n{resp.text}</pre>", 400

    token_data = resp.json()
    token_data["obtained_at"] = time.time()
    _save_token(token_data)

    # Mask token for display
    masked = token_data.get("access_token", "")[:40] + "..."
    return f"""
    <html><body style="font-family:monospace;padding:40px;background:#0a0a0a;color:#00ff88;">
    <h2>&#x2705; Token captured!</h2>
    <p>Access token: {masked}</p>
    <p>Expires in: {token_data.get('expires_in', '?')} seconds</p>
    <p>Refresh token: {'YES' if token_data.get('refresh_token') else 'NO'}</p>
    <br>
    <a href="/schwab/status" style="background:#0066cc;color:white;padding:12px 24px;text-decoration:none;border-radius:6px;">Check Status</a>
    </body></html>
    """


@app.route("/schwab/status")
def schwab_status():
    """Token status check."""
    token = _load_token()
    if not token:
        return """
        <html><body style="font-family:monospace;padding:40px;background:#0a0a0a;color:#ff4444;">
        <h2>No Token</h2>
        <a href="/schwab/auth" style="background:#0066cc;color:white;padding:16px 32px;text-decoration:none;font-size:18px;border-radius:8px;">
          Authorize Schwab
        </a>
        </body></html>
        """

    obtained_at = token.get("obtained_at", 0)
    expires_in  = token.get("expires_in", 1800)
    remaining   = max(0, int(obtained_at + expires_in - time.time()))
    minutes     = remaining // 60

    return f"""
    <html><body style="font-family:monospace;padding:40px;background:#0a0a0a;color:#00ff88;">
    <h2>Schwab Token Status: {'VALID' if remaining > 0 else 'EXPIRED'}</h2>
    <p>Expires in: {minutes} minutes</p>
    <p>Refresh token: {'YES' if token.get('refresh_token') else 'NO'}</p>
    <br>
    <a href="/schwab/auth" style="background:#0066cc;color:white;padding:16px 32px;text-decoration:none;font-size:18px;border-radius:8px;">
      Re-Authorize Schwab
    </a>
    </body></html>
    """


# ── SCHWAB MARKET DATA ROUTES ──────────────────────────────────────────────────

@app.route("/schwab/quotes")
def schwab_quotes():
    """
    Live quotes for one or more symbols.
    Usage: /schwab/quotes?symbols=HTZ,FOXA,WMT,GME
    """
    symbols = request.args.get("symbols", "")
    if not symbols:
        return jsonify({"error": "symbols param required"}), 400

    try:
        access_token = get_valid_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    resp = requests.get(
        f"{SCHWAB_MARKET_URL}/quotes",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"symbols": symbols, "fields": "quote,reference"},
        timeout=10,
    )

    if resp.status_code != 200:
        return jsonify({"error": f"Schwab API {resp.status_code}", "detail": resp.text}), resp.status_code

    data = resp.json()
    # Simplify output to key fields
    result = {}
    for sym, info in data.items():
        q = info.get("quote", {})
        result[sym] = {
            "price":        q.get("lastPrice") or q.get("mark"),
            "change":       q.get("netChange"),
            "change_pct":   q.get("netPercentChangeInDouble"),
            "volume":       q.get("totalVolume"),
            "bid":          q.get("bidPrice"),
            "ask":          q.get("askPrice"),
            "day_high":     q.get("highPrice"),
            "day_low":      q.get("lowPrice"),
            "52w_high":     q.get("52WeekHigh"),
            "52w_low":      q.get("52WeekLow"),
        }

    return jsonify(result), 200


@app.route("/schwab/level2")
def schwab_level2():
    """
    Level 2 order book for a single symbol.
    Usage: /schwab/level2?symbol=HTZ
    """
    symbol = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol param required"}), 400

    try:
        access_token = get_valid_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    resp = requests.get(
        f"{SCHWAB_MARKET_URL}/pricehistory",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "symbol":          symbol,
            "periodType":      "day",
            "period":          1,
            "frequencyType":   "minute",
            "frequency":       1,
            "needExtendedHoursData": False,
        },
        timeout=10,
    )

    if resp.status_code != 200:
        return jsonify({"error": f"Schwab API {resp.status_code}", "detail": resp.text}), resp.status_code

    return jsonify(resp.json()), 200


@app.route("/schwab/keepalive")
def schwab_keepalive():
    """
    Silent token refresh — called by daily cron to prevent 7-day expiry.
    Returns 200 silently if token is valid or successfully refreshed.
    """
    try:
        token = _load_token()
        if not token:
            return jsonify({"status": "no_token"}), 200

        # Force a refresh
        _refresh_access_token(token)
        return jsonify({"status": "refreshed", "ts": datetime.utcnow().isoformat()}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ── EXISTING WEBHOOK + HELPERS (unchanged) ────────────────────────────────────

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
        f"Action: Check Level 2 — buyers stacking = entry confirmed\n\n"
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
    return jsonify({"status": "running", "service": "BMCMS VixFix Webhook + Schwab Market Data"}), 200


@app.route("/test/<ticker>", methods=["GET"])
def test_ticker(ticker):
    ticker = ticker.upper()
    quote  = get_stock_quote(ticker)
    body   = build_notification_body(ticker, "TEST_VixFix_200EMA", quote, "200")
    return jsonify({"ticker": ticker, "quote": quote, "notification_body": body}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
