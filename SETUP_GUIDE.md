# SkyView Investment Advisors – Chatbot Setup Guide

## Step 1: Install Python (Windows)

You got a "pip not recognized" error because Python isn't installed yet.

1. Go to: **https://www.python.org/downloads/**
2. Click the big yellow **"Download Python 3.x.x"** button
3. Run the installer
4. ⚠️ **IMPORTANT:** On the first screen, check the box that says **"Add Python to PATH"** before clicking Install
5. Click **"Install Now"**

Once installed, close PowerShell and reopen it (this refreshes the PATH).

---

## Step 2: Get Your Anthropic API Key

1. Go to: **https://console.anthropic.com**
2. Sign up or log in
3. Navigate to **API Keys** in the left sidebar
4. Click **"Create Key"** and copy it — you'll need it in Step 4

---

## Step 3: Install the Required Packages

Open PowerShell and run:

```powershell
pip install anthropic flask
```

Or install from the requirements file (navigate to the project folder first):

```powershell
cd path\to\skyview_chatbot
pip install -r requirements.txt
```

---

## Step 4: Set Your API Key

In PowerShell, set your API key as an environment variable:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"
```

> 💡 To make this permanent (so you don't have to set it every time), search for
> "Environment Variables" in Windows Settings and add it as a System Variable.

---

## Step 5: Run the Chatbot

In PowerShell, navigate to the project folder and run:

```powershell
cd path\to\skyview_chatbot
python app.py
```

You should see:
```
=======================================================
  SkyView Investment Advisors - Chatbot
  Open your browser at: http://127.0.0.1:5000
=======================================================
```

---

## Step 6: Open the Chat UI

Open your browser and go to: **http://127.0.0.1:5000**

You'll see the full SkyView Claris chatbot with three modes:
- 💬 General Q&A
- 📋 Advisor Tools
- 📊 Portfolio Review

---

## Project Structure

```
skyview_chatbot/
├── app.py              ← Flask web server (runs the app)
├── chatbot.py          ← Claude API integration & SkyView logic
├── requirements.txt    ← Python package list
├── SETUP_GUIDE.md      ← This file
└── templates/
    └── index.html      ← Chat UI (browser interface)
```

---

## Connecting Real Data (Next Steps)

Open `chatbot.py` and look for the `execute_tool()` function.
The three tools have stubs marked with `# STUB` — replace these with real connections:

| Tool | What to Connect |
|------|----------------|
| `analyze_portfolio` | Orion, Tamarac, Morningstar, or your portfolio system |
| `get_market_context` | Bloomberg, Refinitiv, or an internal knowledge base |
| `draft_client_communication` | Already works via Claude — no external connection needed |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `pip not recognized` | Re-install Python and check "Add to PATH" |
| `ModuleNotFoundError: anthropic` | Run `pip install anthropic flask` |
| `AuthenticationError` | Check your API key is set correctly |
| `Port 5000 already in use` | Change `port=5000` in `app.py` to `port=5001` |
