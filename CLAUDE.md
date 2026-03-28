# Claude Code Configuration — Personal Assistant (LangChain)

## Identity — ALWAYS commit as:
```
git config user.name "TalsCodingArea"
git config user.email "Tals.Busi@gmail.com"
```
Set this at the start of EVERY session before any commit. No exceptions.
Never include "Co-Authored-By", "Generated with Claude", or any AI attribution in commit messages.

## This Project Only
This repo is ONLY for the Personal Assistant project.
- Language: Python 3.13
- Stack: LangChain + LangGraph + OpenAI GPT-4o + Telegram Bot API + Notion API
- DO NOT touch or reference files from other projects (career-microsaas, github-profile)
- NO scraper/ directory belongs here — that lives in career-microsaas

## Rules
- NEVER create files unless absolutely necessary
- ALWAYS prefer editing an existing file over creating a new one
- NEVER commit secrets, .env files, or credentials
- ALWAYS run `pip install -r requirements.txt` and verify imports work before committing
- Keep files under 500 lines
- NEVER add a scraper/, frontend/, or backend/ directory here

## File Structure
```
personal-assistant/
├── app.py                    # Telegram bot entry point
├── agent/
│   ├── builder.py            # LangChain agent setup
│   ├── budget_workflow.py    # LangGraph budget workflow
│   ├── contexts/             # Per-intent system prompts
│   ├── llm.py
│   └── memory.py
├── tools/
│   ├── notion_tools.py
│   ├── receipt_tools.py
│   ├── job_tools.py
│   ├── movie_tools.py
│   ├── web_search_tools.py   # DuckDuckGo search
│   ├── israeli_market_tools.py
│   └── registry.py
├── router/
│   └── intent_router.py
├── automation_functions.py   # Zero-arg automation functions
├── base_scripts.py
├── budget_data/              # financial_goals.json, repeating_categories.json
├── resume_data/              # Jinja2 templates
├── requirements.txt
└── docker-compose.yml
```

## Build / Test
```bash
pip install -r requirements.txt
python -c "import app"  # verify no import errors
```

## Concurrency
- Batch ALL related operations in ONE message
- Use swarm: hierarchical topology, max 6 agents, specialized strategy

## Git Workflow
```bash
git config user.name "TalsCodingArea"
git config user.email "Tals.Busi@gmail.com"
git add -A
git commit -m "feat/fix/chore: descriptive message"
git push origin main
```
