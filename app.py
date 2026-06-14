"""
BMCMC LLC — Railway Bridge Server v2
--------------------------------------
Endpoints:
  GET  /health                  — health check (Schwab auth status)
  GET  /system/status           — full system status (bridge + Bloomberg + Schwab)

  -- SCHWAB --
  GET  /schwab/auth             — initiate Schwab OAuth flow
  GET  /schwab/debug            — OAuth callback / token capture
  GET  /schwab/status           — token status check
  GET  /schwab/keepalive        — silent token refresh
  GET  /schwab/quotes           — live quotes: ?symbols=HTZ,FOXA,WMT
  GET  /schwab/level2           — Level 2 order book: ?symbol=HTZ
  GET  /schwab/positions        — all open positions across all accounts
  GET  /schwab/accounts         — account balances (net liq, buying power, P/L)
  GET  /schwab/options          — options chain: ?symbol=HTZ&expiration=2027-01-15
  GET  /schwab/history          — closed trade history: ?days=90&symbol=HTZ (optional)

  -- BLOOMBERG --
  POST /bloomberg/askb          — run AskB query via PowerShell on Windows machine
  POST /bloomberg/altd          — pull ALTD data for a ticker via Excel bridge
  POST /bloomberg/bdp           — run single BDP field pull

  -- FILES (Windows machine filesystem) --
  POST /files/read              — read a file from Windows machine by path
  POST /files/list              — list directory contents on Windows machine
  POST /files/write             — write/create a file on Windows machine

  -- WEBHOOK --
  POST /webhook                 — TrendSpider VixFix alert receiver
  GET  /test/<ticker>           — test notification

  -- LEGAL --
  GET  /privacy                 — BMCMC LLC Privacy Policy
  GET  /terms                   — BMCMC LLC Terms of Service
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

# ── CREDENTIALS (all via Railway environment variables) ───────────────────────
FINVIZ_AUTH           = os.environ.get("FINVIZ_AUTH", "")
SCHWAB_CLIENT_ID      = os.environ.get("SCHWAB_CLIENT_ID", "")
SCHWAB_CLIENT_SECRET  = os.environ.get("SCHWAB_CLIENT_SECRET", "")
SCHWAB_CALLBACK_URL   = os.environ.get("SCHWAB_CALLBACK_URL", "https://web-production-76c25d.up.railway.app/schwab/debug")
BRIDGE_SECRET         = os.environ.get("BRIDGE_SECRET", "")
BRIDGE_HOST           = os.environ.get("BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT           = int(os.environ.get("BRIDGE_PORT", 8765))
TWILIO_ACCOUNT_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_TO             = os.environ.get("TWILIO_TO", "")
TWILIO_FROM           = os.environ.get("TWILIO_FROM", "")

SCHWAB_AUTH_URL    = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_TOKEN_URL   = "https://api.schwabapi.com/v1/oauth/token"
SCHWAB_MARKET_URL  = "https://api.schwabapi.com/marketdata/v1"
SCHWAB_TRADER_URL  = "https://api.schwabapi.com/trader/v1"

_token_store = {}
TOKEN_FILE = "/tmp/schwab_token.json"

EMA_LABELS = {
    "50":  "GOOD — 50 EMA",
    "100": "STRONG — 100 EMA",
    "200": "NUCLEAR — 200 EMA",
}


# ── BRIDGE HELPER (calls Windows machine via TCP tunnel) ───────────────────────

def run_on_windows(cmd: str, timeout: int = 45) -> dict:
    """Send a shell command to the TN Bridge Server on the Windows machine."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((BRIDGE_HOST, BRIDGE_PORT))
        msg = json.dumps({"secret": BRIDGE_SECRET, "cmd": cmd})
        s.sendall(msg.encode() + b"\n")
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            try:
                json.loads(data.decode().strip())
                break
            except json.JSONDecodeError:
                continue
        s.close()
        return json.loads(data.decode().strip())
    except Exception as e:
        return {"error": str(e), "stdout": "", "stderr": "", "returncode": -1}


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
    if "refresh_token" not in new_token:
        new_token["refresh_token"] = refresh_token
    _save_token(new_token)
    return new_token


def get_valid_token() -> str:
    token = _load_token()
    if not token:
        raise RuntimeError("No token available — re-authorize at /schwab/auth")
    obtained_at = token.get("obtained_at", 0)
    expires_in  = token.get("expires_in", 1800)
    if time.time() > obtained_at + expires_in - 300:
        token = _refresh_access_token(token)
    return token["access_token"]


# ── SYSTEM STATUS ──────────────────────────────────────────────────────────────

@app.route("/system/status")
def system_status():
    """Full system health check — Railway + Windows bridge + Bloomberg + Schwab."""
    result = {
        "timestamp": datetime.utcnow().isoformat(),
        "railway": "ok",
        "schwab_auth": False,
        "schwab_token_minutes_remaining": 0,
        "windows_bridge": False,
        "bloomberg_reachable": False,
        "bridge_error": None,
    }

    # Schwab token check
    token = _load_token()
    if token:
        obtained_at = token.get("obtained_at", 0)
        expires_in  = token.get("expires_in", 1800)
        remaining   = max(0, int(obtained_at + expires_in - time.time()))
        result["schwab_auth"] = remaining > 0
        result["schwab_token_minutes_remaining"] = remaining // 60

    # Windows bridge ping
    bridge_result = run_on_windows("echo bridge_ok", timeout=10)
    if bridge_result.get("stdout", "").strip() == "bridge_ok":
        result["windows_bridge"] = True
    else:
        result["bridge_error"] = bridge_result.get("error", "no response")

    # Bloomberg reachable (check if bbg_altd_pull.ps1 exists)
    if result["windows_bridge"]:
        bbg_check = run_on_windows(
            'if (Test-Path "C:\\Users\\TNap7\\bbg_altd_pull.ps1") { echo "bbg_ok" } else { echo "bbg_missing" }',
            timeout=15
        )
        result["bloomberg_reachable"] = "bbg_ok" in bbg_check.get("stdout", "")

    return jsonify(result), 200


# ── HEALTH CHECK ───────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    token = _load_token()
    schwab_auth = bool(token)
    if schwab_auth:
        obtained_at = token.get("obtained_at", 0)
        expires_in  = token.get("expires_in", 1800)
        schwab_auth = time.time() < obtained_at + expires_in

    return jsonify({
        "status":      "ok",
        "schwab_auth": schwab_auth,
        "timestamp":   datetime.utcnow().isoformat(),
    }), 200


# ── FILES — WINDOWS MACHINE FILESYSTEM ────────────────────────────────────────

@app.route("/files/read", methods=["POST"])
def files_read():
    """
    Read a file from the Windows machine.
    Body: {"path": "C:\\Users\\TNap7\\some_file.pdf", "secret": "BGSM2024"}
    Returns: {"content": "...", "size_bytes": 1234, "encoding": "text|base64"}
    """
    data = request.get_json(silent=True) or {}
    if data.get("secret") != BRIDGE_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    path = data.get("path", "")
    if not path:
        return jsonify({"error": "path required"}), 400

    # Sanitize — only allow paths under known safe directories
    safe_prefixes = [
        "C:\\Users\\TNap7\\",
        "C:\\Bloomberg\\",
        "C:\\Users\\TNap7\\Documents\\",
    ]
    if not any(path.startswith(p) for p in safe_prefixes):
        return jsonify({"error": f"path must be under: {safe_prefixes}"}), 403

    # Read as base64 to handle binary files (PDFs etc)
    ps_cmd = (
        f'$bytes = [System.IO.File]::ReadAllBytes("{path}"); '
        f'[Convert]::ToBase64String($bytes)'
    )
    result = run_on_windows(ps_cmd, timeout=30)

    if result.get("error"):
        return jsonify({"error": result["error"]}), 500

    b64 = result.get("stdout", "").strip()
    if not b64:
        return jsonify({"error": "file not found or empty", "detail": result.get("stderr", "")}), 404

    return jsonify({
        "path":      path,
        "content":   b64,
        "encoding":  "base64",
        "size_bytes": len(base64.b64decode(b64))
    }), 200


@app.route("/files/list", methods=["POST"])
def files_list():
    """
    List directory contents on the Windows machine.
    Body: {"path": "C:\\Users\\TNap7\\", "secret": "BGSM2024"}
    Returns: {"files": [...], "dirs": [...]}
    """
    data = request.get_json(silent=True) or {}
    if data.get("secret") != BRIDGE_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    path = data.get("path", "C:\\Users\\TNap7\\")

    ps_cmd = (
        f'$items = Get-ChildItem -Path "{path}" -ErrorAction SilentlyContinue; '
        f'$out = @{{files=@();dirs=@()}}; '
        f'foreach ($i in $items) {{ '
        f'  if ($i.PSIsContainer) {{ $out.dirs += $i.Name }} '
        f'  else {{ $out.files += @{{name=$i.Name; size=$i.Length; modified=$i.LastWriteTime.ToString("yyyy-MM-dd HH:mm")}} }} '
        f'}}; '
        f'$out | ConvertTo-Json -Depth 3'
    )
    result = run_on_windows(ps_cmd, timeout=20)

    if result.get("error"):
        return jsonify({"error": result["error"]}), 500

    try:
        listing = json.loads(result.get("stdout", "{}"))
    except json.JSONDecodeError:
        listing = {"raw": result.get("stdout", "")}

    return jsonify({"path": path, **listing}), 200


@app.route("/files/write", methods=["POST"])
def files_write():
    """
    Write a file to the Windows machine.
    Body: {"path": "C:\\Users\\TNap7\\file.txt", "content": "base64...", "secret": "BGSM2024"}
    """
    data = request.get_json(silent=True) or {}
    if data.get("secret") != BRIDGE_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    path    = data.get("path", "")
    content = data.get("content", "")  # base64 encoded
    if not path or not content:
        return jsonify({"error": "path and content required"}), 400

    safe_prefixes = ["C:\\Users\\TNap7\\"]
    if not any(path.startswith(p) for p in safe_prefixes):
        return jsonify({"error": "path must be under C:\\Users\\TNap7\\"}), 403

    ps_cmd = (
        f'$bytes = [Convert]::FromBase64String("{content}"); '
        f'[System.IO.File]::WriteAllBytes("{path}", $bytes); '
        f'echo "write_ok"'
    )
    result = run_on_windows(ps_cmd, timeout=30)

    if "write_ok" in result.get("stdout", ""):
        return jsonify({"status": "ok", "path": path}), 200
    else:
        return jsonify({"error": result.get("stderr", "write failed")}), 500


# ── BLOOMBERG ──────────────────────────────────────────────────────────────────

@app.route("/bloomberg/askb", methods=["POST"])
def bloomberg_askb():
    """
    Run an AskB query on Bloomberg terminal via PowerShell.
    Body: {"query": "ALTD PLACER", "secret": "BGSM2024"}
    Returns raw AskB output from Bloomberg.
    """
    data = request.get_json(silent=True) or {}
    if data.get("secret") != BRIDGE_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    query = data.get("query", "")
    if not query:
        return jsonify({"error": "query required"}), 400

    # Use PowerShell to interact with Bloomberg AskB via COM automation
    ps_cmd = (
        f'Add-Type -Path "C:\\blp\\API\\APIv3\\CSharpAPI\\v3.14.3.1\\Bloomberglp.Blpapi.dll" 2>$null; '
        f'$session = [Bloomberglp.Blpapi.SessionOptions]::new(); '
        f'$session.ServerHost = "localhost"; '
        f'$session.ServerPort = 8194; '
        f'Write-Output "AskB query: {query} — Run this manually in Bloomberg terminal: ASKB <GO> then type: {query}"'
    )
    result = run_on_windows(ps_cmd, timeout=30)

    return jsonify({
        "query":    query,
        "result":   result.get("stdout", ""),
        "error":    result.get("stderr", "") or result.get("error", ""),
        "note":     "AskB requires manual Bloomberg terminal interaction. Use /bloomberg/bdp for programmatic field pulls."
    }), 200


@app.route("/bloomberg/altd", methods=["POST"])
def bloomberg_altd():
    """
    Pull ALTD data for a ticker via Excel bridge on Windows machine.
    Body: {"ticker": "KR", "secret": "BGSM2024"}
    Runs bbg_altd_pull.ps1 which opens Excel, refreshes Bloomberg, returns JSON.
    """
    data = request.get_json(silent=True) or {}
    if data.get("secret") != BRIDGE_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    ticker = data.get("ticker", "").upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400

    # Update the ticker in the Excel template first, then run the pull script
    ps_cmd = (
        f'cd C:\\Users\\TNap7; '
        f'$env:BBG_TICKER = "{ticker}"; '
        f'powershell -ExecutionPolicy Bypass -File C:\\Users\\TNap7\\bbg_altd_pull.ps1 -Ticker {ticker} 2>&1'
    )
    result = run_on_windows(ps_cmd, timeout=120)

    stdout = result.get("stdout", "")

    # Try to parse JSON from stdout
    try:
        # Find JSON block in output
        start = stdout.find("{")
        end   = stdout.rfind("}") + 1
        if start >= 0 and end > start:
            altd_data = json.loads(stdout[start:end])
            return jsonify({
                "ticker": ticker,
                "altd":   altd_data,
                "status": "ok"
            }), 200
    except json.JSONDecodeError:
        pass

    return jsonify({
        "ticker": ticker,
        "raw":    stdout,
        "error":  result.get("stderr", "") or result.get("error", ""),
        "status": "raw_output"
    }), 200


@app.route("/bloomberg/bdp", methods=["POST"])
def bloomberg_bdp():
    """
    Pull a single BDP field from Bloomberg via Excel bridge.
    Body: {"ticker": "KR US Equity", "field": "PX_LAST", "secret": "BGSM2024"}
    """
    data = request.get_json(silent=True) or {}
    if data.get("secret") != BRIDGE_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    ticker = data.get("ticker", "")
    field  = data.get("field", "PX_LAST")
    if not ticker:
        return jsonify({"error": "ticker required"}), 400

    # Use Python blpapi on Windows machine
    ps_cmd = (
        f'cd C:\\Users\\TNap7; '
        f'python bbg_altd.py --ticker "{ticker}" --field "{field}" 2>&1'
    )
    result = run_on_windows(ps_cmd, timeout=60)

    return jsonify({
        "ticker": ticker,
        "field":  field,
        "result": result.get("stdout", "").strip(),
        "error":  result.get("stderr", "") or result.get("error", "")
    }), 200


# ── SCHWAB AUTH ROUTES ─────────────────────────────────────────────────────────

@app.route("/schwab/auth")
def schwab_auth():
    auth_url = (
        f"{SCHWAB_AUTH_URL}"
        f"?response_type=code"
        f"&client_id={SCHWAB_CLIENT_ID}"
        f"&redirect_uri={SCHWAB_CALLBACK_URL}"
    )
    return redirect(auth_url)


@app.route("/schwab/debug")
def schwab_debug():
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


@app.route("/schwab/keepalive")
def schwab_keepalive():
    try:
        get_valid_token()
        return jsonify({"status": "ok", "message": "Token refreshed"}), 200
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 401


# ── SCHWAB MARKET DATA ─────────────────────────────────────────────────────────

@app.route("/schwab/quotes")
def schwab_quotes():
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
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "symbol param required"}), 400

    try:
        access_token = get_valid_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    resp = requests.get(
        f"{SCHWAB_MARKET_URL}/quotes",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"symbols": symbol, "fields": "quote,reference"},
        timeout=10,
    )

    if resp.status_code != 200:
        return jsonify({"error": f"Schwab API {resp.status_code}", "detail": resp.text}), resp.status_code

    return jsonify(resp.json()), 200


# ── SCHWAB TRADER ─────────────────────────────────────────────────────────────

@app.route("/schwab/positions")
def schwab_positions():
    """All open positions across all accounts."""
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

        acct_data = r.json()
        positions = acct_data.get("securitiesAccount", {}).get("positions", [])

        for pos in positions:
            instrument = pos.get("instrument", {})
            symbol     = instrument.get("symbol", "")
            asset_type = instrument.get("assetType", "")
            desc       = instrument.get("description", "")

            long_qty  = pos.get("longQuantity", 0)
            short_qty = pos.get("shortQuantity", 0)
            qty = long_qty if long_qty else -short_qty

            avg_price    = pos.get("averagePrice", 0)
            market_value = pos.get("marketValue", 0)
            pl_open      = pos.get("longOpenProfitLoss", 0) or pos.get("shortOpenProfitLoss", 0)
            pl_day       = pos.get("currentDayProfitLoss", 0)

            all_positions.append({
                "account":      acct_num[-4:] if acct_num else "????",
                "symbol":       symbol,
                "description":  desc,
                "asset_type":   asset_type,
                "qty":          qty,
                "avg_price":    round(avg_price, 4),
                "market_value": round(market_value, 2),
                "pl_open":      round(pl_open, 2),
                "pl_day":       round(pl_day, 2),
            })

    return jsonify({"positions": all_positions, "count": len(all_positions)}), 200


@app.route("/schwab/accounts")
def schwab_accounts():
    """Account balances: net liq, buying power, P/L day."""
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
    results = []

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

        acct_data = r.json().get("securitiesAccount", {})
        balances  = acct_data.get("currentBalances", {})

        results.append({
            "account":         acct_num[-4:] if acct_num else "????",
            "type":            acct_data.get("type", ""),
            "net_liquidation": round(balances.get("liquidationValue", 0), 2),
            "buying_power":    round(balances.get("buyingPower", 0) or balances.get("availableFunds", 0), 2),
            "cash_balance":    round(balances.get("cashBalance", 0), 2),
            "day_pl":          round(balances.get("dayTradingEquityCall", 0), 2),
            "equity":          round(balances.get("equity", 0), 2),
        })

    return jsonify({"accounts": results}), 200


@app.route("/schwab/history")
def schwab_history():
    """
    Closed trade history from Schwab.
    Params:
      days   — number of days back (default 90, max 365)
      symbol — optional filter by symbol (e.g. HTZ)
    Returns list of closed transactions with entry/exit price and P/L.
    """
    try:
        access_token = get_valid_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    days   = min(int(request.args.get("days", 90)), 365)
    symbol = request.args.get("symbol", "").upper()

    end_date   = datetime.utcnow()
    start_date = end_date - timedelta(days=days)

    resp = requests.get(
        f"{SCHWAB_TRADER_URL}/accounts/accountNumbers",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        return jsonify({"error": f"accountNumbers {resp.status_code}", "detail": resp.text}), resp.status_code

    account_numbers = resp.json()
    all_transactions = []

    for acct in account_numbers:
        hash_val = acct.get("hashValue")
        acct_num = acct.get("accountNumber")
        if not hash_val:
            continue

        params = {
            "startDate": start_date.strftime("%Y-%m-%dT00:00:00.000Z"),
            "endDate":   end_date.strftime("%Y-%m-%dT23:59:59.000Z"),
            "types":     "TRADE",
        }
        if symbol:
            params["symbol"] = symbol

        r = requests.get(
            f"{SCHWAB_TRADER_URL}/accounts/{hash_val}/transactions",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=30,
        )
        if r.status_code != 200:
            continue

        transactions = r.json() if isinstance(r.json(), list) else r.json().get("transactions", [])

        for txn in transactions:
            txn_type   = txn.get("type", "")
            txn_date   = txn.get("tradeDate", txn.get("settlementDate", ""))
            net_amount = txn.get("netAmount", 0)

            transfer_items = txn.get("transferItems", [])
            for item in transfer_items:
                instrument  = item.get("instrument", {})
                sym         = instrument.get("symbol", "")
                asset_type  = instrument.get("assetType", "")
                desc        = instrument.get("description", "")
                quantity    = item.get("amount", 0)
                price       = item.get("price", 0)
                cost        = item.get("cost", 0)
                instruction = item.get("positionEffect", "")  # OPENING / CLOSING

                if symbol and sym.upper() != symbol:
                    continue

                all_transactions.append({
                    "account":     acct_num[-4:] if acct_num else "????",
                    "date":        txn_date,
                    "type":        txn_type,
                    "symbol":      sym,
                    "description": desc,
                    "asset_type":  asset_type,
                    "quantity":    quantity,
                    "price":       round(price, 4),
                    "cost":        round(cost, 2),
                    "net_amount":  round(net_amount, 2),
                    "position_effect": instruction,
                })

    # Sort by date descending
    all_transactions.sort(key=lambda x: x.get("date", ""), reverse=True)

    return jsonify({
        "transactions": all_transactions,
        "count":        len(all_transactions),
        "date_range":   f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}",
        "symbol_filter": symbol or "ALL"
    }), 200


# ── SCHWAB OPTIONS CHAIN ───────────────────────────────────────────────────────

@app.route("/schwab/options")
def schwab_options():
    symbol        = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol param required"}), 400

    expiration    = request.args.get("expiration", None)
    contract_type = request.args.get("contract_type", "ALL").upper()
    strike_count  = int(request.args.get("strike_count", 20))

    try:
        access_token = get_valid_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    params = {
        "symbol":        symbol,
        "contractType":  contract_type,
        "strikeCount":   strike_count,
        "includeUnderlyingQuote": True,
        "strategy":      "SINGLE",
    }
    if expiration:
        params["fromDate"] = expiration
        params["toDate"]   = expiration

    resp = requests.get(
        f"{SCHWAB_MARKET_URL}/chains",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=15,
    )

    if resp.status_code != 200:
        return jsonify({"error": f"Schwab API {resp.status_code}", "detail": resp.text}), resp.status_code

    raw = resp.json()

    underlying_price = None
    uq = raw.get("underlyingQuote") or raw.get("underlying") or {}
    underlying_price = uq.get("last") or uq.get("mark") or uq.get("close")

    result = {
        "symbol":           symbol,
        "underlying_price": underlying_price,
        "status":           raw.get("status"),
        "expiration_dates": list(raw.get("callExpDateMap", {}).keys()),
        "calls":            {},
        "puts":             {},
    }

    def parse_leg(exp_map):
        parsed = {}
        for exp_key, strikes in exp_map.items():
            exp_date = exp_key.split(":")[0]
            parsed[exp_date] = {}
            for strike_str, contracts in strikes.items():
                strike = float(strike_str)
                c = contracts[0] if contracts else {}
                parsed[exp_date][strike] = {
                    "bid":         c.get("bid"),
                    "ask":         c.get("ask"),
                    "last":        c.get("last"),
                    "mark":        c.get("mark"),
                    "iv":          round(c.get("volatility", 0), 4) if c.get("volatility") else None,
                    "delta":       round(c.get("delta", 0), 4) if c.get("delta") else None,
                    "theta":       round(c.get("theta", 0), 4) if c.get("theta") else None,
                    "oi":          c.get("openInterest"),
                    "volume":      c.get("totalVolume"),
                    "itm":         c.get("inTheMoney"),
                    "description": c.get("description"),
                }
        return parsed

    if contract_type in ("CALL", "ALL"):
        result["calls"] = parse_leg(raw.get("callExpDateMap", {}))
    if contract_type in ("PUT", "ALL"):
        result["puts"] = parse_leg(raw.get("putExpDateMap", {}))

    return jsonify(result), 200


# ── WEBHOOK ────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data    = request.get_json(silent=True) or {}
    ticker  = data.get("ticker", "UNKNOWN")
    price   = data.get("price", "?")
    ema     = str(data.get("ema", "50"))
    vixfix  = data.get("vixfix", "?")
    pct     = data.get("pct", "?")
    volume  = data.get("volume", "?")

    label = EMA_LABELS.get(ema, f"{ema} EMA")
    title = f"EMA SIGNAL — {ticker} | {label}"
    body  = (
        f"Price ${price} | {label} touch | "
        f"VixFix {vixfix} ({pct}th pct) | Vol {volume}x avg\n"
        f"Jan 2027 calls, 10 contracts, limit mid or below"
    )

    try:
        if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            subprocess.run([
                "curl", "-s", "-X", "POST",
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
                "--user", f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}",
                "--data-urlencode", f"To={TWILIO_TO}",
                "--data-urlencode", f"From={TWILIO_FROM}",
                "--data-urlencode", f"Body={title}\n{body}",
            ], timeout=10)
    except Exception:
        pass

    return jsonify({"status": "ok", "ticker": ticker}), 200


# ── TEST ───────────────────────────────────────────────────────────────────────

@app.route("/test/<ticker>")
def test_alert(ticker):
    title = f"TEST — {ticker} | EMA Signal"
    body  = f"This is a test alert for {ticker}. System operational."
    return jsonify({"status": "ok", "title": title, "body": body}), 200


# ── LEGAL ──────────────────────────────────────────────────────────────────────

@app.route("/privacy")
def privacy():
    return """<html><body style="font-family:Arial;padding:40px;max-width:800px;">
    <h1>Privacy Policy — BMCMC LLC</h1>
    <p>Last updated: June 2, 2026</p>
    <p>BMCMC LLC ("we", "us") operates trading alert and notification services. We collect only the phone numbers necessary to deliver SMS alerts to authorized users. We do not sell or share personal information with third parties. SMS messages are sent solely for trading signal notifications requested by the account holder. To opt out, reply STOP to any message.</p>
    <p>Contact: connect@aemgworldwide.com</p>
    </body></html>"""


@app.route("/terms")
def terms():
    return """<html><body style="font-family:Arial;padding:40px;max-width:800px;">
    <h1>Terms of Service — BMCMC LLC</h1>
    <p>Last updated: June 2, 2026</p>
    <p>By using BMCMC LLC notification services, you agree that: (1) SMS alerts are for informational purposes only and do not constitute financial advice; (2) trading involves risk and past signals do not guarantee future results; (3) you are solely responsible for all trading decisions; (4) service availability is not guaranteed. BMCMC LLC is not liable for any trading losses.</p>
    <p>Contact: connect@aemgworldwide.com</p>
    </body></html>"""


# ── ENTRY POINT ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
