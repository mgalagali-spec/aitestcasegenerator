# Jira QA Test Plan Generator

Generates a test plan from a Jira ticket using rules only (no external AI).

## Setup (PowerShell)

```powershell
cd jira-qa-agent
python -m venv venv
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env     # then edit .env and paste your token
python test_jira.py FL-7641   # checks login, ticket access, and finds the AC custom field
streamlit run app.py
```

Files: `app.py` (UI + Jira), `qa_engine.py` (rules), `test_jira.py` (connection check).

## Getting the most accurate plans

The engine is only as good as the ticket. It works best when tickets have:

- an **Acceptance Criteria** heading (or an AC custom field) with one criterion per bullet;
- concrete numbers – "max 50 characters", "between 1 and 99", "within 2 seconds" – which become boundary/performance tests;
- named roles – "Only admins can…", "Viewers see it read-only" – which become access tests;
- Gherkin (Given/When/Then) where possible – steps and expected results are taken directly from it;
- for bugs: **Steps to Reproduce**, **Expected Result**, **Actual Result** headings.

The "Open questions" panel lists vague or untestable criteria – send these back to the ticket owner.

`.env` must never be committed or shared. Add it to `.gitignore`.

## AI mode (realistic test cases)

Rules-only mode fills templates, so its steps are generic. AI mode writes ticket-specific steps
and concrete test data, grounded by the rule engine's analysis (AC IDs, limits, roles, gaps).
Any criterion the AI skips is filled with the rule-based tests, and if the AI call fails the
rule-based plan is shown instead.

Configure one option in `.env`:

- **OpenAI:** `OPENAI_API_KEY=...` (optionally `OPENAI_MODEL=...`). Ticket text is sent to OpenAI.
- **Local Ollama (nothing leaves your PC):** install Ollama, run `ollama pull llama3.1`, then set
  `OPENAI_BASE_URL=http://localhost:11434/v1` and `OPENAI_MODEL=llama3.1`.

Switch between AI and rules-only mode in the app's sidebar.
