# Claris Multi-Persona Platform — Integration Guide

## Quick Start

### 1. Copy New Files into Your Repo

Copy the following files/folders alongside your existing `app.py` and `chatbot.py`:

```
skyview-claris/
├── app.py                  ← EXISTING (modify — see step 2)
├── chatbot.py              ← EXISTING (no changes)
├── app_persona.py          ← NEW
├── persona_engine.py       ← NEW
├── models.py               ← NEW
├── knowledge.py            ← NEW
├── compliance.py           ← NEW
├── seed_data.py            ← NEW
├── alembic.ini             ← NEW
├── requirements.txt        ← UPDATED
├── admin/                  ← NEW (entire folder)
│   └── __init__.py
├── templates/              ← ADD to existing
│   ├── persona.html
│   ├── persona_login.html
│   └── admin/
│       ├── base_admin.html
│       ├── dashboard.html
│       ├── personas.html
│       ├── persona_form.html
│       ├── persona_test.html
│       ├── knowledge.html
│       ├── compliance.html
│       ├── employees.html
│       ├── conversations.html
│       └── conversation_detail.html
└── migrations/             ← NEW (entire folder)
    ├── env.py
    ├── script.py.mako
    └── versions/
```

### 2. Add Two Lines to Your app.py

At the **bottom** of your existing `app.py`, right before `if __name__ == "__main__":`, add:

```python
# ── Multi-Persona Platform ─────────────────────────
from app_persona import init_multipersona
init_multipersona(app)
```

That's it. Your existing `/`, `/advisor`, `/chat/stream` routes stay exactly the same.

### 3. Set Environment Variables

On Render.com (or locally), add these environment variables:

```bash
# Required
DATABASE_URL=postgresql://user:pass@host:5432/claris_multipersona
ANTHROPIC_API_KEY=sk-ant-...          # Already set

# Azure AD SSO (get from Azure Portal → App Registrations)
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret
AZURE_TENANT_ID=your-tenant-id
AZURE_REDIRECT_URI=https://your-app.onrender.com/auth/callback

# Embeddings (for RAG — pick one)
VOYAGE_API_KEY=your-voyage-key        # Preferred
# OPENAI_API_KEY=your-openai-key      # Fallback

# Flask
FLASK_SECRET_KEY=a-random-32-byte-string
FLASK_ENV=production
```

### 4. Provision PostgreSQL on Render

1. Go to Render Dashboard → New → PostgreSQL
2. Name: `claris-multipersona-db`
3. Plan: Starter ($7/mo) or Standard ($20/mo)
4. Copy the **Internal Database URL** and set it as `DATABASE_URL`

Enable pgvector:
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### 5. Install Dependencies & Seed Data

```bash
pip install -r requirements.txt
python seed_data.py
```

### 6. Register Azure AD App (for SSO)

1. Go to Azure Portal → Azure Active Directory → App Registrations → New
2. Name: `SkyView Claris`
3. Redirect URI: `https://your-app.onrender.com/auth/callback`
4. Under Certificates & Secrets → New client secret → copy value
5. Set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`

**Dev Mode:** If you skip Azure setup, the login page shows an email-only form
for development testing. Set `FLASK_ENV=development` to enable it.

### 7. Deploy

```bash
git add .
git commit -m "Add multi-persona platform"
git push origin main
```

Render will auto-deploy. Your routes:

| Route | Purpose |
|-------|---------|
| `/` | Client portal (existing, unchanged) |
| `/advisor` | Advisor tool (existing, unchanged) |
| `/persona` | Multi-persona chat (NEW) |
| `/persona/login` | SSO login (NEW) |
| `/admin` | Admin dashboard (NEW) |

## Architecture

```
Request → /persona/chat/stream
  ↓
SSO Auth (Azure AD) → employee_id
  ↓
Load Persona (PostgreSQL) → persona record
  ↓
Assemble System Prompt:
  [L4 Compliance] + [L1 Firm] + [L2 Persona] + [L3 Knowledge] + [Workflow]
  ↓
Claude API (streaming SSE, tool use)
  ↓
Post-Processing (compliance filters, disclaimer injection)
  ↓
Stream to Browser + Save to DB
```
