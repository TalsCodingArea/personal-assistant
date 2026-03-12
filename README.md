# 🤖 Personal Assistant

A self-hosted AI-powered personal assistant running on a Raspberry Pi, built with LangChain and connected to Telegram. It manages finances, tracks movies, handles job applications, processes receipts, and runs daily automations — all through a simple chat interface.

---

## ✨ Features

### 💸 Finance Tracking
- Log expenses and income to Notion automatically
- Process receipts via PDF upload — extracts vendor, date, amount, and category using GPT-4o (with OCR fallback for scanned receipts)
- Monthly and weekly spending summaries
- Real-time budget evaluation after every logged expense

### 🎬 Movie Tracker
- Add movies to a Notion watchlist with genres and AI-generated mood tags
- Log watches and ratings
- Get AI-powered movie suggestions based on your mood

### 💼 Job Applications
- Send a job URL → get a tailored resume, cover letter PDF, and personal note — all generated automatically
- Company research via DuckDuckGo + LLM synthesis
- Logs every application to a Notion jobs database

### 🧾 Receipt Processing
- Send a receipt PDF to the Telegram receipts channel
- Auto-extracts and categorizes the data, uploads to Notion, and evaluates the spend

### ⚙️ Automations
- `morning_summary` — Daily performance recap based on your Notion day scores and workout streaks
- `get_weekly_spending_summary` — Weekly finance overview
- `evaluate_expense` — Inline budget check after each new expense

---

## 🏗️ Architecture

```
Telegram Bot (app.py)
    │
    ├── Intent Router → finance / movies / job_application / general
    │
    ├── Conversational Agent (LangChain)
    │       ├── Tools: Notion CRUD, receipt OCR, movie search, ideas
    │       └── Memory: per-session chat history keyed by chat_id
    │
    ├── Job Application Pipeline (bypasses agent)
    │       scrape → parse → research → generate docs → log to Notion
    │
    └── Automations Channel
            → maps message text to no-arg functions
```

**Stack:** Python 3.13 · LangChain · OpenAI GPT-4o / GPT-4o-mini · Notion API · Telegram Bot API · WeasyPrint · BeautifulSoup

---

## 📁 Project Structure

```
├── app.py                        # Telegram bot entry point
├── agent/
│   ├── builder.py                # LangChain agent setup
│   ├── contexts/                 # Per-intent system prompts
│   └── llm.py                   # LLM configuration
├── tools/
│   ├── notion_tools.py           # Notion DB CRUD
│   ├── receipt_tools.py          # PDF OCR pipeline
│   ├── job_tools.py              # Job application workflow
│   ├── movie_tools.py            # Movie search & logging
│   └── registry.py              # Tool registration
├── router/
│   └── intent_router.py          # Message intent classifier
├── automation_functions.py       # Scheduled/triggered automations
├── base_scripts.py               # Shared utilities (email, Notion, OpenAI)
├── resume_data/
│   ├── resume_template.html      # Jinja2 resume template
│   └── cover_letter_template.html
└── docker-compose.yml
```

---

## 🚀 Setup

### 1. Clone & install dependencies

```bash
git clone https://github.com/TalsCodingArea/personal-assistant.git
cd personal-assistant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy and fill in your `.env` file:

```env
# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID_PERSONAL_ASSISTANT=
TELEGRAM_CHAT_ID_RECEIPTS=
TELEGRAM_CHAT_ID_LOGS=
TELEGRAM_CHAT_ID_AUTOMATIONS=

# OpenAI
OPENAI_API_KEY=
ASSISTANT_LLM_MODEL=gpt-4o-mini
ASSISTANT_LLM_TEMPERATURE=0.7

# Notion
NOTION_API_KEY=
EXPENSES_DATABASE_ID=
INCOME_DATABASE_ID=
MOVIES_DATABASE_ID=
JOBS_DATABASE_ID=

# Notion — Automations
DAY_RATING_DATABASE_ID=
WORKOUTS_DATABASE_ID=

# Email (Gmail SMTP)
GMAIL_EMAIL=
GMAIL_APP_PASSWORD=
THINGS_EMAIL=

# Other
OMDB_API_KEY=
PDF_ENDPOINT_ACCESS_TOKEN=
RECEIPT_CATEGORY_OPTIONS=Groceries,Restaurant,Bills,EV,Online Services,Therapy,Decor
```

### 3. Prepare personal data files

These files are gitignored — you must create them locally:

| File | Description |
|---|---|
| `resume_data/user_profile.json` | Your personal info, experience, skills |
| `personal_notes_examples/*.txt` | Writing samples for few-shot note generation |
| `notion_config/databases.json` | Notion DB IDs map |
| `notion_config/finance_rules.json` | Budget % targets |

### 4. Run

```bash
python app.py
```

Or with Docker:
```bash
docker-compose up
```

---

## 📬 Telegram Channels

| Channel | Purpose |
|---|---|
| `personal_assistant` | General chat, finance, movies, job applications |
| `receipts` | Drop a receipt PDF here to auto-log it |
| `automations` | Send a function name to trigger it (e.g. `morning_summary`) |
| `logs` | System output and confirmations |

---

## 🗺️ Roadmap

- [ ] **Calendar read access** — Query upcoming events from Google Calendar
- [ ] **Academic tasks integration** — Pull tasks and deadlines from academic sources
- [ ] **Smart study scheduler** — Analyze academic tasks and auto-book "Study Session" slots in the calendar based on priority and available time

---

## 🛠️ Raspberry Pi System Dependencies

Required for WeasyPrint (PDF generation):

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
                 libgdk-pixbuf2.0-0 libffi-dev shared-mime-info
```
