"""
AI test-case writer.

The rule engine (qa_engine.py) still does the parsing: it finds the acceptance
criteria, numbers them AC-1..n, detects boundaries/roles/APIs and flags vague
wording. That analysis is handed to an LLM as grounding, and the LLM writes
realistic test cases with concrete steps and test data. Output is validated, and
if anything goes wrong the rule-based plan is returned unchanged.

Works with any OpenAI-compatible chat API:
  - OpenAI:          OPENAI_API_KEY=sk-...           (OPENAI_BASE_URL unset)
  - Ollama (local):  OPENAI_BASE_URL=http://localhost:11434/v1   OPENAI_MODEL=llama3.1
  - Azure/other gateways: set OPENAI_BASE_URL to the gateway's /v1 URL
"""
from __future__ import annotations

import json
import os
import re

import requests

from qa_engine import adf_to_text, clean_markup

VALID_TYPES = ["Positive", "Negative", "Boundary", "Edge Case", "Security", "Regression", "Performance", "UI"]
VALID_PRIORITIES = ["Critical", "High", "Medium", "Low"]

SYSTEM_PROMPT = """You are a senior QA engineer writing manual test cases that a tester will execute tomorrow.

Write test cases that are specific to THIS ticket:
- Use the real screen names, field names, buttons, roles, endpoints and messages from the ticket.
- Every step is one concrete action a tester performs ("Enter 'priya.k@example.com' in Email"), never
  a vague instruction ("Perform the action", "Verify it works", "Navigate to the feature").
- Give realistic test data with actual values (names, emails, amounts, dates, file names). For limits,
  use the exact boundary values (e.g. 49, 50 and 51 characters for a 50-character limit).
- Expected results are observable and checkable: what is shown, saved, sent, or returned. Quote exact
  messages only if the ticket gives them; otherwise describe the message ("an error under the Email field").
- Cover each acceptance criterion with at least one test, then add negative, boundary and edge cases
  that are genuinely relevant to this feature. Do NOT add generic tests (SQL injection, network drop,
  403 checks) unless the ticket's feature makes them relevant.
- Do not invent features or rules the ticket doesn't mention. If you must assume something, put it in
  "assumptions" and write the test anyway.
- Vary the tests: no two test cases should read like the same template with one word changed.
- For bugs: reproduce the original defect first, then variations, then regression around the fix.

Return ONLY a JSON object, no markdown, with this shape:
{
  "test_cases": [
    {
      "title": "short, specific title",
      "type": one of %(types)s,
      "priority": one of %(priorities)s,
      "covers": ["AC-1"],
      "preconditions": "specific setup, accounts and data",
      "test_data": "concrete values used (or empty string)",
      "steps": ["step 1", "step 2"],
      "expected_result": "observable outcome"
    }
  ],
  "assumptions": ["..."],
  "open_questions": ["questions for the product owner about gaps in the ticket"]
}""" % {"types": VALID_TYPES, "priorities": VALID_PRIORITIES}


def ai_settings() -> dict:
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    local = bool(re.match(r"https?://(localhost|127\.0\.0\.1)", base))
    return {
        "base_url": base,
        "api_key": os.getenv("OPENAI_API_KEY", "ollama" if local else ""),
        "model": os.getenv("OPENAI_MODEL", "llama3.2" if local else "gpt-4o-mini"),
        "local": local,
        # Local CPU models are slow, so ask for less and wait longer.
        "max_cases": int(os.getenv("AI_MAX_CASES", "8" if local else "20")),
        "max_tokens": int(os.getenv("AI_MAX_TOKENS", "2500" if local else "6000")),
        "timeout": int(os.getenv("AI_TIMEOUT", "600" if local else "180")),
        "max_desc_chars": 4000 if local else 12000,
    }


def ai_available() -> tuple[bool, str]:
    s = ai_settings()
    if not s["api_key"]:
        return False, "Set OPENAI_API_KEY in .env (or OPENAI_BASE_URL for a local Ollama server)."
    where = "on this machine" if s["local"] else f"to {s['base_url']}"
    return True, f"Model {s['model']} – ticket text is sent {where}."


def _build_user_prompt(ticket: dict, rule_plan: dict, max_desc_chars: int = 12000) -> str:
    raw = ticket.get("description") or ""
    text = clean_markup(adf_to_text(raw) if not isinstance(raw, str) else raw).strip()
    criteria = "\n".join(f"{c['id']}: {c['text']}" + (f" (details: {'; '.join(c['details'])})" if c["details"] else "")
                         for c in rule_plan["criteria"]) or "(none found – infer from the description)"
    checks = "\n".join(f"- {tc['title']} [{tc['basis']}]" for tc in rule_plan["test_cases"]
                       if not tc["title"].startswith("AC-"))
    parts = [
        f"Ticket: {ticket.get('key')} – {ticket.get('summary')}",
        f"Type: {ticket.get('issue_type') or 'unknown'}",
    ]
    if ticket.get("components"):
        parts.append(f"Components: {', '.join(ticket['components'])}")
    if ticket.get("labels"):
        parts.append(f"Labels: {', '.join(ticket['labels'])}")
    parts += [
        "", "Full description:", text[:max_desc_chars] or "(empty)",
        "", "Acceptance criteria (use these IDs in 'covers'):", criteria,
        "", "Checks a rule engine detected in the ticket (use the relevant ones, written specifically):",
        checks or "(none)",
        "", "Ticket gaps already noticed:", "\n".join(f"- {r}" for r in rule_plan["risks"]) or "(none)",
    ]
    return "\n".join(parts)


def _salvage_cases(text: str) -> dict:
    """Output cut off by the length cap: keep every test case object that finished."""
    start = text.find("[", text.find('"test_cases"'))
    if start < 0:
        raise ValueError("The AI response was not valid JSON.")
    dec, pos, cases = json.JSONDecoder(), start + 1, []
    while True:
        nxt = text.find("{", pos)
        if nxt < 0:
            break
        try:
            obj, end = dec.raw_decode(text, nxt)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            cases.append(obj)
        pos = end
    if not cases:
        raise ValueError("The AI response was cut off before any test case was complete.")
    return {"test_cases": cases, "assumptions": [],
            "open_questions": ["AI output was cut off at the length limit – some test cases may be missing."]}


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return _salvage_cases(text)


def _clean_case(tc: dict, valid_ids: set) -> dict | None:
    steps = tc.get("steps") or []
    if isinstance(steps, str):
        steps = [s for s in re.split(r"\n+|\s*\d+\.\s+", steps) if s.strip()]
    steps = [re.sub(r"^\s*(?:step\s*)?\d+[.):]\s*", "", str(s)).strip() for s in steps if str(s).strip()]
    title = str(tc.get("title", "")).strip()
    if not title or not steps:
        return None
    ttype = next((t for t in VALID_TYPES if t.lower() == str(tc.get("type", "")).strip().lower()), "Positive")
    prio = next((p for p in VALID_PRIORITIES if p.lower() == str(tc.get("priority", "")).strip().lower()), "Medium")
    covers = tc.get("covers") or []
    if isinstance(covers, str):
        covers = re.findall(r"AC-\d+", covers)
    covers = [c for c in covers if c in valid_ids]
    data = tc.get("test_data", "")
    if isinstance(data, (list, dict)):
        data = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else "; ".join(map(str, data))
    return {
        "title": title, "type": ttype, "priority": prio, "covers": covers,
        "preconditions": str(tc.get("preconditions", "")).strip() or "–",
        "test_data": str(data).strip(),
        "steps": steps, "expected_result": str(tc.get("expected_result", "")).strip() or "–",
        "basis": "AI-written from ticket" + (f" ({', '.join(covers)})" if covers else ""),
    }


def generate_ai_plan(ticket: dict, rule_plan: dict, timeout: int | None = None) -> dict:
    """Returns a copy of rule_plan with AI-written test cases. Raises on failure."""
    s = ai_settings()
    limit = (f"\n\nWrite at most {s['max_cases']} test cases: one per acceptance criterion first, then the most "
             f"valuable negative/boundary cases. Keep each step short.")
    resp = requests.post(
        f"{s['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {s['api_key']}", "Content-Type": "application/json"},
        json={
            "model": s["model"],
            "temperature": 0.2,
            "max_tokens": s["max_tokens"],
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT + limit},
                {"role": "user", "content": _build_user_prompt(ticket, rule_plan, s["max_desc_chars"])},
            ],
        },
        timeout=(10, timeout or s["timeout"]),
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"AI request failed ({resp.status_code}): {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)

    valid_ids = {c["id"] for c in rule_plan["criteria"]}
    cases = [c for c in (_clean_case(tc, valid_ids) for tc in data.get("test_cases", []) if isinstance(tc, dict)) if c]
    if not cases:
        raise RuntimeError("The AI response contained no usable test cases.")

    # Safety net: any criterion the model skipped gets the rule engine's tests for it
    covered = {cid for tc in cases for cid in tc["covers"]}
    for c in rule_plan["criteria"]:
        if c["id"] not in covered:
            for tc in rule_plan["test_cases"]:
                if c["id"] in tc["covers"] and tc not in cases:
                    cases.append(dict(tc, basis=f"Rule-based fallback – AI did not cover {c['id']}"))
                    covered.update(tc["covers"])
    for i, tc in enumerate(cases, 1):
        tc["id"] = f"TC-{i:03d}"

    plan = dict(rule_plan)
    plan["test_cases"] = cases
    plan["traceability"] = {cid: [tc["id"] for tc in cases if cid in tc["covers"]] for cid in valid_ids}
    plan["traceability"] = {c["id"]: plan["traceability"][c["id"]] for c in rule_plan["criteria"]}
    extra = [f"Assumption: {a}" for a in data.get("assumptions", []) if str(a).strip()]
    extra += [str(q) for q in data.get("open_questions", []) if str(q).strip()]
    plan["risks"] = list(dict.fromkeys(rule_plan["risks"] + extra))
    plan["generator"] = f"AI ({s['model']})"
    return plan
