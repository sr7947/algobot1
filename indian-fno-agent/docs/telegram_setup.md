# Telegram Bot Setup Guide

This guide walks you through creating a new Telegram bot and configuring it for the F&O Trading Agent.

---

## Step 1: Create a New Bot via BotFather

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a **name** for your bot (e.g., `My F&O Trading Agent`)
4. Choose a **username** ending with `bot` (e.g., `my_fno_agent_bot`)
5. BotFather will respond with your **Bot Token**:
   ```
   Use this token to access the HTTP API:
   7123456789:AAH1234567890abcdefghijklmnop
   ```
6. **Copy and save this token** — you'll need it for `.env`

---

## Step 2: Get Your Chat ID

The bot must know YOUR chat ID to restrict access:

1. Start a chat with your new bot (search for its username, click "Start")
2. Send any message to the bot (e.g., "hello")
3. Open this URL in your browser (replace `YOUR_BOT_TOKEN`):
   ```
   https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
   ```
4. Look for the `"chat"` object in the JSON response:
   ```json
   "chat": {
     "id": 123456789,
     "first_name": "Kumar",
     "type": "private"
   }
   ```
5. **Copy the `id` number** — this is your Chat ID

---

## Step 3: Configure .env

Add these values to your `.env` file:

```env
# Telegram Configuration
TELEGRAM_BOT_TOKEN=7123456789:AAH1234567890abcdefghijklmnop
TELEGRAM_CHAT_ID=123456789
TELEGRAM_APPROVAL_TIMEOUT_MINUTES=15
```

---

## Step 4: Optional — Set Bot Commands

The bot automatically registers commands on startup, but you can also set them manually:

1. Open @BotFather
2. Send `/setcommands`
3. Select your bot
4. Paste:
   ```
   start - Start bot & welcome
   status - System status & daily P&L
   positions - Open positions
   signals - Pending signals
   pnl - Today's P&L summary
   killswitch - Emergency stop all trading
   resume - Resume after kill switch
   mode - Switch paper/live mode
   help - Show all commands
   ```

---

## Step 5: Test the Bot

```bash
# Start the trading agent
uvicorn api.main:app --host 0.0.0.0 --port 8000

# The Telegram bot starts automatically with the API
# Go to Telegram and send /start to your bot
```

You should see:
```
🤖 Indian F&O Trading Agent

Mode: PAPER
Broker: angel_one
Status: Online ✅

Use /help for all commands.
```

---

## How Trade Approval Works

When a signal passes risk checks, the bot sends a message like this:

```
📊 TRADE SIGNAL

🟢 BUY NIFTY 24000 CE
Strategy: Options Momentum
Confidence: 78%

Entry: ₹145.00
Stop Loss: ₹101.50
Target: ₹217.50
Risk:Reward: 1:1.67

Regime: Trending Bull
PCR: 0.75 (Bullish)

📰 Positive FII inflow data

[✅ Approve] [❌ Reject] [📉 Half Size] [🚫 Block]
```

- **✅ Approve**: Places the order at full size
- **❌ Reject**: Cancels the signal
- **📉 Half Size**: Approves but with 50% of recommended quantity
- **🚫 Block**: Rejects AND blocks similar signals for 30 minutes

If no response within `TELEGRAM_APPROVAL_TIMEOUT_MINUTES` (default: 15), the signal expires automatically.

---

## Troubleshooting

| Issue | Solution |
|---|---|
| Bot not responding | Check `TELEGRAM_BOT_TOKEN` is correct |
| "Unauthorized" message | Your `TELEGRAM_CHAT_ID` doesn't match |
| No signals received | Check that strategies are running and generating signals |
| Buttons not working | Ensure the bot has inline keyboard permissions |
