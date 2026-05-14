# TrendSpider VixFix Webhook Receiver

Receives TrendSpider alert webhooks when a VixFix + EMA loading zone fires.
Pulls live quote + Finviz chart, then pushes a Perplexity notification.

## Deploy on Railway

1. Go to railway.app → New Project → Deploy from GitHub (or upload this folder)
2. Railway auto-detects Python + Procfile
3. Once deployed, copy the public URL (e.g. https://your-app.railway.app)

## TrendSpider Setup

### Step 1 — Alert Note (JSON payload)
In your VixFix alert's "Note" field, paste this exactly:
```
{"symbol": "%alert_symbol%", "alert": "%alert_name%"}
```

### Step 2 — Alert Name Convention
Name your alerts to include the EMA level so the webhook knows which tier fired:
- `VixFix_50EMA` → GOOD
- `VixFix_100EMA` → STRONG  
- `VixFix_200EMA` → NUCLEAR

### Step 3 — Webhook URL
In TrendSpider → Settings → Notifications → Webhook for alerts, paste:
```
https://your-app.railway.app/webhook
```

## Test It
Hit: `https://your-app.railway.app/test/HTZ`
Should return a full quote + notification body for HTZ.

## Health Check
`https://your-app.railway.app/health`
