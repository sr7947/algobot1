# Gemini API Setup Guide

The trading agent uses Google's **Gemini 1.5 Flash** for AI-powered market analysis, sentiment analysis, and regime detection.

---

## Step 1: Create a Google Cloud Project (or use AI Studio)

### Option A: Google AI Studio (Simpler)

1. Go to [https://aistudio.google.com/](https://aistudio.google.com/)
2. Sign in with your Google account
3. Click **"Get API Key"** in the left sidebar
4. Click **"Create API key"**
5. Select or create a Google Cloud project
6. Copy the generated API key

### Option B: Google Cloud Console

1. Go to [https://console.cloud.google.com/](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Navigate to **APIs & Services > Library**
4. Search for **"Generative Language API"** and enable it
5. Go to **APIs & Services > Credentials**
6. Click **"Create Credentials" > "API Key"**
7. Copy the API key

---

## Step 2: Configure .env

```env
# Gemini API Configuration
GEMINI_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## Step 3: Verify API Key

```python
import google.generativeai as genai

genai.configure(api_key="YOUR_API_KEY_HERE")
model = genai.GenerativeModel("gemini-1.5-flash")
response = model.generate_content("Say hello in Hindi")
print(response.text)
```

Expected output: Something like `नमस्ते! (Namaste!)`

---

## How Gemini is Used

The agent uses Gemini in several modules:

| Module | Purpose | Model |
|---|---|---|
| `agents/technical_agent.py` | Multi-timeframe technical analysis | gemini-1.5-flash |
| `agents/options_agent.py` | Options flow interpretation | gemini-1.5-flash |
| `agents/news_agent.py` | News sentiment classification | gemini-1.5-flash |
| `agents/regime_agent.py` | Market regime detection | gemini-1.5-flash |
| `agents/risk_arbiter.py` | Final risk assessment | gemini-1.5-flash |
| `news/sentiment.py` | Headline sentiment scoring | gemini-1.5-flash |

---

## Rate Limits & Costs

### Free Tier (AI Studio)
- **15 requests per minute** (RPM)
- **1 million tokens per minute** (TPM)
- **1,500 requests per day** (RPD)
- The agent includes a built-in rate limiter (10 req/min default)

### Pricing (if exceeding free tier)
- Gemini 1.5 Flash: ~$0.075 per 1M input tokens
- Typical daily usage: 500–2000 requests ≈ negligible cost

---

## Fallback Behaviour

If the Gemini API is unavailable or rate-limited:
- **Sentiment analysis** falls back to keyword-based scoring
- **Technical analysis** uses pure indicator-based rules (no LLM)
- **Signal generation** continues with lower confidence scores
- All fallbacks are logged for audit

---

## Troubleshooting

| Issue | Solution |
|---|---|
| `403 Forbidden` | API key doesn't have Generative Language API enabled |
| `429 Rate Limited` | Reduce `NEWS_FETCH_INTERVAL_MINUTES` or upgrade plan |
| `API key not valid` | Regenerate key from AI Studio or Cloud Console |
| Slow responses | Gemini Flash is optimized for speed; check network |
