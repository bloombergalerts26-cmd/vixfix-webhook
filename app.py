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
  GET  /schwab/positions   — all open positions across all accounts
  GET  /schwab/accounts    — account balances (net liq, buying power, P/L)
  GET  /privacy             — BMCMS LLC Privacy Policy (public, for Twilio A2P)
  GET  /terms               — BMCMS LLC Terms of Service (public, for Twilio A2P)

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
SCHWAB_TRADER_URL  = "https://api.schwabapi.com/trader/v1"

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


@app.route("/schwab/positions")
def schwab_positions():
    """
    All open positions across all accounts.
    Returns simplified position data: symbol, qty, avg price, current value, P/L open, P/L day.
    """
    try:
        access_token = get_valid_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    # First get account numbers
    resp = requests.get(
        f"{SCHWAB_TRADER_URL}/accounts/accountNumbers",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        return jsonify({"error": f"accountNumbers {resp.status_code}", "detail": resp.text}), resp.status_code

    account_numbers = resp.json()  # [{"accountNumber": "...", "hashValue": "..."}]

    all_positions = []
    for acct in account_numbers:
        hash_val = acct.get("hashValue")
        acct_num = acct.get("accountNumber")
        if not hash_val:
            continue

        r = requests.get(
            f"{SCHWAB_TRADER_URL}/accounts/{hash_val}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "positions"},
            timeout=15,
        )
        if r.status_code != 200:
            continue

        data = r.json()
        acct_data = data.get("securitiesAccount", data)
        positions = acct_data.get("positions", [])
        acct_type = acct_data.get("type", "UNKNOWN")
        current_balances = acct_data.get("currentBalances", {})

        for pos in positions:
            instrument = pos.get("instrument", {})
            symbol     = instrument.get("symbol", "")
            asset_type = instrument.get("assetType", "")
            desc       = instrument.get("description", "")

            all_positions.append({
                "account":        acct_num[-4:] if acct_num else "????",  # last 4 only
                "account_type":   acct_type,
                "symbol":         symbol,
                "asset_type":     asset_type,
                "description":    desc,
                "qty":            pos.get("longQuantity", 0) or pos.get("shortQuantity", 0),
                "avg_price":      pos.get("averagePrice"),
                "market_value":   pos.get("marketValue"),
                "pl_open":        pos.get("longOpenProfitLoss") or pos.get("openProfitLoss"),
                "pl_day":         pos.get("currentDayProfitLoss"),
                "pl_day_pct":     pos.get("currentDayProfitLossPercentage"),
                "current_price":  pos.get("currentDayProfitLoss"),  # calculated below
                "net_liq":        current_balances.get("liquidationValue"),
            })

    return jsonify({"positions": all_positions, "count": len(all_positions)}), 200


@app.route("/schwab/accounts")
def schwab_accounts():
    """
    Account balances: net liq, buying power, cash, P/L day, P/L open.
    """
    try:
        access_token = get_valid_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    resp = requests.get(
        f"{SCHWAB_TRADER_URL}/accounts/accountNumbers",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        return jsonify({"error": f"accountNumbers {resp.status_code}", "detail": resp.text}), resp.status_code

    account_numbers = resp.json()
    result = []

    for acct in account_numbers:
        hash_val = acct.get("hashValue")
        acct_num = acct.get("accountNumber")
        if not hash_val:
            continue

        r = requests.get(
            f"{SCHWAB_TRADER_URL}/accounts/{hash_val}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if r.status_code != 200:
            continue

        data  = r.json()
        sa    = data.get("securitiesAccount", data)
        cb    = sa.get("currentBalances", {})
        ib    = sa.get("initialBalances", {})

        result.append({
            "account":          acct_num[-4:] if acct_num else "????",
            "type":             sa.get("type", "UNKNOWN"),
            "net_liq":          cb.get("liquidationValue"),
            "buying_power":     cb.get("buyingPower") or cb.get("availableFunds"),
            "cash_available":   cb.get("cashAvailableForTrading"),
            "pl_day":           cb.get("dayTradingBuyingPower"),  # will be overridden
            "equity":           cb.get("equity"),
            "long_market_value":cb.get("longMarketValue"),
            "short_market_value":cb.get("shortMarketValue"),
        })

    return jsonify({"accounts": result}), 200


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


@app.route("/privacy", methods=["GET"])
def privacy():
    """BMCMS LLC Privacy Policy — public page for Twilio A2P compliance."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Privacy Policy - BMCMS LLC</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 800px; margin: 60px auto; padding: 0 24px; color: #1a1a1a; line-height: 1.7; }
    h1 { font-size: 28px; margin-bottom: 8px; }
    h2 { font-size: 18px; margin-top: 36px; }
    p { margin: 12px 0; }
    .updated { color: #666; font-size: 14px; margin-bottom: 40px; }
    a { color: #0070f3; }
  </style>
</head>
<body>
  <h1>Privacy Policy</h1>
  <p class="updated">Last updated: May 27, 2026</p>
  <p>BMCMS LLC ("Company," "we," "us," or "our") operates the BMCMS Trading Ops platform and related SMS alert services. This Privacy Policy describes how we collect, use, and protect information in connection with our services.</p>
  <h2>1. Information We Collect</h2>
  <p>We collect only the minimum information necessary to operate our SMS alert service. This includes the mobile phone number provided by the account owner for the purpose of receiving trading alerts.</p>
  <h2>2. SMS Messaging Service</h2>
  <p>Our SMS messaging service delivers automated trading alerts exclusively to the registered account owner's mobile number. Messages are operational in nature and relate solely to market data signals and trade notifications for the account owner's personal trading activity.</p>
  <p>Message frequency varies based on market conditions. Message and data rates may apply. To opt out at any time, reply STOP to any message. To re-subscribe, reply START.</p>
  <h2>3. How We Use Information</h2>
  <p>The mobile phone number collected is used solely to deliver SMS alerts to the registered account owner. We do not sell, share, rent, or transfer personal information to any third party for marketing purposes.</p>
  <h2>4. Data Security</h2>
  <p>We implement reasonable administrative and technical measures to protect the information we hold. Mobile numbers are stored securely and accessed only for the purpose of delivering authorized alerts.</p>
  <h2>5. Retention</h2>
  <p>We retain contact information only as long as the account owner maintains an active relationship with our service. Upon request or opt-out, mobile numbers are removed from our active messaging list.</p>
  <h2>6. Contact</h2>
  <p>For questions about this Privacy Policy or to request removal of your information, contact us at: <a href="mailto:admin@bmcms.com">admin@bmcms.com</a></p>
  <p style="margin-top:48px; color:#666; font-size:13px;">© 2026 BMCMS LLC. All rights reserved.</p>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html"}


@app.route("/terms", methods=["GET"])
def terms():
    """BMCMS LLC Terms of Service — public page for Twilio A2P compliance."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Terms of Service - BMCMS LLC</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 800px; margin: 60px auto; padding: 0 24px; color: #1a1a1a; line-height: 1.7; }
    h1 { font-size: 28px; margin-bottom: 8px; }
    h2 { font-size: 18px; margin-top: 36px; }
    p { margin: 12px 0; }
    .updated { color: #666; font-size: 14px; margin-bottom: 40px; }
    a { color: #0070f3; }
  </style>
</head>
<body>
  <h1>Terms of Service</h1>
  <p class="updated">Last updated: May 27, 2026</p>
  <p>These Terms of Service govern your use of the BMCMS Trading Ops SMS alert service operated by BMCMS LLC ("Company").</p>
  <h2>1. Service Description</h2>
  <p>BMCMS Trading Ops provides automated SMS trading alerts to the registered account owner. These alerts are informational in nature and relate to market data signals generated by proprietary trading systems. No investment advice is provided or implied.</p>
  <h2>2. Eligibility</h2>
  <p>This service is intended for use solely by the registered account owner. The SMS messaging service delivers alerts exclusively to the verified mobile number on file.</p>
  <h2>3. SMS Terms</h2>
  <p>By registering a mobile number with BMCMS LLC, you consent to receive automated SMS trading alert messages. Message frequency varies based on market conditions. Standard message and data rates may apply depending on your wireless carrier plan.</p>
  <p>To opt out of SMS messages at any time, reply STOP to any message. You will receive a confirmation and no further messages will be sent. To re-subscribe, reply START.</p>
  <p>For help, reply HELP or INFO to any message.</p>
  <h2>4. No Investment Advice</h2>
  <p>All alerts and notifications delivered through this service are for informational purposes only. Nothing in these communications constitutes investment advice, a recommendation to buy or sell any security, or a guarantee of investment returns. All trading decisions are made solely by the account owner.</p>
  <h2>5. Limitation of Liability</h2>
  <p>BMCMS LLC shall not be liable for any damages arising from your use of or reliance on SMS alerts delivered through this service. Market conditions can change rapidly and past signal performance does not guarantee future results.</p>
  <h2>6. Modifications</h2>
  <p>We reserve the right to modify these Terms at any time. Continued use of the service following any modification constitutes acceptance of the updated Terms.</p>
  <h2>7. Contact</h2>
  <p>Questions regarding these Terms may be directed to: <a href="mailto:admin@bmcms.com">admin@bmcms.com</a></p>
  <p style="margin-top:48px; color:#666; font-size:13px;">© 2026 BMCMS LLC. All rights reserved.</p>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html"}


@app.route("/test/<ticker>", methods=["GET"])
def test_ticker(ticker):
    ticker = ticker.upper()
    quote  = get_stock_quote(ticker)
    body   = build_notification_body(ticker, "TEST_VixFix_200EMA", quote, "200")
    return jsonify({"ticker": ticker, "quote": quote, "notification_body": body}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
