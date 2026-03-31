# Personal AI Phone Assistant

Call a real phone number and talk to an AI that knows your Gmail and Google Calendar.

**Stack:** [AgentPhone.to](https://agentphone.to) + Claude (Anthropic) + Gmail API + Google Calendar API

---

## How It Works

```
You call your phone number
        ↓
AgentPhone transcribes your voice
        ↓
This server receives the text via webhook
        ↓
Claude reads your email/calendar and generates a response
        ↓
Response streams back sentence-by-sentence via ndjson
        ↓
AgentPhone speaks each sentence as it arrives (low latency)
```

You can say things like:
- "What's on my calendar today?"
- "Do I have any unread emails?"
- "Any emails from my boss this week?"
- "What's coming up this week?"
- "Transfer me to John" (if you configure transfer contacts)

---

## Features

- **Streaming responses** — Sentences stream via ndjson as Claude generates them, so AgentPhone can start text-to-speech on the first sentence immediately
- **Smart tool routing** — Only sends email/calendar tools to Claude when relevant keywords are detected, keeping casual chat fast (~0.5-1s)
- **Gmail batch API** — Fetches multiple emails in a single API call instead of N+1 individual requests
- **Call transfer** — Say "transfer me to [name]" to transfer the call to a configured contact, with STT alias support for misheard names
- **Hangup detection** — Say "goodbye" or "hang up" to end the call
- **Date awareness** — Claude always knows today's date for accurate calendar responses
- **Timing logs** — Detailed per-turn latency tracking (TTFT, TTFS, total) for debugging
- **SMS support** — Text your number and get responses too

---

## Setup Guide (Step by Step)

### Step 1: Get Your API Keys

You need three things:

**A) Anthropic API Key** (for Claude)
1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Sign up / log in
3. Go to API Keys → Create Key
4. Copy it — starts with `sk-ant-...`

**B) AgentPhone API Key**
1. Go to [agentphone.to](https://agentphone.to)
2. Sign up (free, no credit card)
3. Go to your dashboard → API Keys
4. Create and copy your key — starts with `ap_...`

**C) Google OAuth Credentials** (for Gmail + Calendar access)
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable these APIs:
   - Gmail API → [Enable here](https://console.cloud.google.com/apis/library/gmail.googleapis.com)
   - Google Calendar API → [Enable here](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
4. Go to **APIs & Services → Credentials**
5. Click **Create Credentials → OAuth client ID**
6. Choose **Desktop app** as the application type
7. Download the JSON file and rename it to `credentials.json`
8. Place `credentials.json` in this project folder

> **Note:** The first time you run the server, it will open a browser window asking
> you to log in to your Google account and grant read-only access. After that,
> a `token.json` file is saved so you won't need to do it again.

### Step 2: Install Dependencies

```bash
# Clone this repo, then:
cd Personal-AI-Phone-Assistant

# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate

# Install packages
pip install -r requirements.txt
```

### Step 3: Configure Environment Variables

```bash
# Copy the template
cp .env.example .env

# Edit .env and add your Anthropic API key
# ANTHROPIC_API_KEY=sk-ant-...
```

### Step 4: Deploy Your Server (Get a Public URL)

AgentPhone needs to reach your webhook over the internet. Pick one:

**Option A: Railway (easiest for beginners)**
1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables: `ANTHROPIC_API_KEY` and `GOOGLE_TOKEN_JSON` (see Step 6)
4. Railway gives you a public URL like `https://your-app.up.railway.app`

**Option B: Render**
1. Go to [render.com](https://render.com) → New Web Service
2. Connect your GitHub repo
3. Set build command: `pip install -r requirements.txt`
4. Set start command: `gunicorn server:app --bind 0.0.0.0:8000`
5. Add environment variables

**Option C: ngrok (for local testing)**
```bash
# In one terminal, run the server:
python server.py

# In another terminal:
ngrok http 8000
# This gives you a public URL like https://abc123.ngrok.io
```

Your webhook URL will be: `https://YOUR_DOMAIN/webhook`

### Step 5: Set Up AgentPhone (Get Your Phone Number)

```bash
# Set your AgentPhone key and webhook URL
export AGENTPHONE_API_KEY="ap_your_key_here"
export WEBHOOK_URL="https://your-app.up.railway.app/webhook"

# Run the setup script
python setup_agentphone.py
```

This will:
- Create an AI agent on AgentPhone (webhook voice mode)
- Provision a real US phone number
- Point it at your webhook

### Step 6: Authenticate with Google (One-Time)

```bash
# Make sure credentials.json is in this folder, then:
python -c "from server import get_google_credentials; get_google_credentials()"
```

A browser window will open asking you to sign in to Google. Grant read-only access
to Gmail and Calendar. A `token.json` file will be created — this caches your login.

**For Railway / hosted deploys:** Copy the contents of `token.json` into a
`GOOGLE_TOKEN_JSON` environment variable on your host:
```bash
# On Railway, set this env var to the contents of token.json:
cat token.json  # copy the output, paste as GOOGLE_TOKEN_JSON in Railway dashboard
```

### Step 7: Call Your Number!

Pick up your phone, dial the number from Step 5, and ask:
- "What's on my calendar today?"
- "Read me my recent emails"
- "Any emails from John this week?"

---

## Project Structure

```
Personal-AI-Phone-Assistant/
├── server.py              # Main webhook server (Flask) — streaming ndjson responses
├── setup_agentphone.py    # One-time setup: creates agent + provisions phone number
├── gunicorn.conf.py       # Gunicorn config (gthread workers for streaming)
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── credentials.json       # Google OAuth credentials (you create this)
├── token.json             # Google OAuth token (auto-generated)
└── README.md              # This file
```

---

## Customization

### Transfer Contacts

Edit `TRANSFER_CONTACTS` in `server.py` to add people you can transfer calls to:

```python
TRANSFER_CONTACTS = {
    "john": "+11234567890",
    "sarah": "+10987654321",
}
```

You'll also need to set `transferNumber` on your agent via the AgentPhone API or dashboard.

Since speech-to-text can mishear names, add aliases in `TRANSFER_ALIASES`:

```python
TRANSFER_ALIASES = {
    "jon": "john",
    "sharah": "sarah",
}
```

### Other Ideas

- **Add more tools** — Give Claude access to your to-do list, Slack, Notion, etc.
- **Change the personality** — Edit `_system_prompt()` in server.py
- **Outbound calls** — Use AgentPhone's `POST /v1/calls` to have your assistant call you with a morning briefing
- **More email detail** — Expand the email fetcher to include full message bodies

---

## Troubleshooting

**"Gmail is not connected"**
→ Make sure `credentials.json` is in the project folder and run the Google auth step (Step 6)

**"Missing ANTHROPIC_API_KEY"**
→ Set it in your `.env` file or as an environment variable

**Phone number doesn't respond**
→ Check your server logs. Make sure your webhook URL is publicly accessible.
→ Test with: `curl https://YOUR_DOMAIN/health`

**Google token expired**
→ Delete `token.json` and re-run Step 6

**Slow responses**
→ Make sure gunicorn is using `gthread` workers (check for `Using worker: gthread` in logs). The included `gunicorn.conf.py` handles this automatically.

---

## Performance

Typical latency with Claude Haiku + gthread workers:

| Turn Type | Server Response Time |
|-----------|---------------------|
| Casual chat | 0.5–1.0s |
| Calendar check | 1.5–2.5s |
| Email search | 2.0–3.0s |
| Transfer | Instant |

Time-to-first-sentence (TTFS) is typically **0.5–0.8s** — AgentPhone can start speaking before Claude finishes generating.

---

## Cost Estimates

| Service | Cost |
|---------|------|
| AgentPhone | Free tier (1st number: 1,000 SMS + 250 voice min/month) |
| Claude API | ~$0.003–0.015 per conversation turn |
| Google APIs | Free (Gmail + Calendar read access) |
| Hosting | Free tier on Railway/Render, or ~$5/month |

For personal use, expect to spend **under $5/month total**.
