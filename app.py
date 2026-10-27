import os
import re
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from jira import JIRA

from ai_engine import ai_available, generate_ai_plan
from qa_engine import adf_to_text, generate_test_plan, to_csv, to_jira_wiki, to_markdown

load_dotenv(Path(__file__).with_name(".env"), override=True)


# --- Jira connection ----------------------------------------------------------

def _ssl_verify() -> bool:
    """Use the Windows/macOS certificate store (works behind corporate proxies)
    instead of turning SSL verification off. Set JIRA_VERIFY_SSL=false only as a last resort."""
    verify = os.getenv("JIRA_VERIFY_SSL", "true").strip().lower() not in ("0", "false", "no")
    if verify:
        try:
            import truststore
            truststore.inject_into_ssl()
        except ImportError:
            pass
    else:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return verify


@st.cache_resource(show_spinner=False)
def get_jira_client():
    url, email, token = os.getenv("JIRA_URL"), os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")
    if not (url and email and token):
        raise ValueError("JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN must be set in the .env file.")
    return JIRA(server=url, basic_auth=(email, token), options={"verify": _ssl_verify()})


@st.cache_data(ttl=3600, show_spinner=False)
def acceptance_criteria_field_ids():
    """Many Jira projects keep AC in a custom field. Use JIRA_AC_FIELD if set, otherwise find it by name."""
    configured = os.getenv("JIRA_AC_FIELD", "").strip()
    if configured:
        return [f.strip() for f in configured.split(",") if f.strip()]
    try:
        return [f["id"] for f in get_jira_client().fields()
                if re.search(r"acceptance\s*criteria|definition\s+of\s+done", f.get("name", ""), re.I)]
    except Exception:
        return []


def fetch_ticket(ticket_key: str) -> dict:
    jira = get_jira_client()
    ac_fields = acceptance_criteria_field_ids()
    wanted = ["summary", "description", "issuetype", "priority", "labels", "components", "status"] + ac_fields
    issue = jira.issue(ticket_key.strip().upper(), fields=",".join(wanted))
    f = issue.fields

    description = adf_to_text(f.description) if f.description else ""
    for fid in ac_fields:
        value = getattr(f, fid, None)
        if value:
            # Put the custom-field AC under its own heading so the engine parses it as AC
            description += "\n\nh3. Acceptance Criteria\n" + adf_to_text(value if not hasattr(value, "raw") else value.raw)

    return {
        "key": issue.key,
        "summary": f.summary,
        "description": description,
        "issue_type": f.issuetype.name if f.issuetype else "",
        "priority": f.priority.name if getattr(f, "priority", None) else "",
        "status": f.status.name if getattr(f, "status", None) else "",
        "labels": list(getattr(f, "labels", None) or []),
        "components": [c.name for c in (getattr(f, "components", None) or [])],
    }


def post_comment_to_jira(ticket_key: str, comment_text: str):
    get_jira_client().add_comment(ticket_key, comment_text)


# --- UI ---------------------------------------------------------------------------

st.set_page_config(page_title="QA Test Plan Generator", page_icon="🧪", layout="wide")

# Optional password screen (set APP_PASSWORD in .env or Streamlit secrets)
_pw = os.getenv("APP_PASSWORD")
if _pw and not st.session_state.get("authed"):
    _entered = st.text_input("Password", type="password")
    if _entered == _pw:
        st.session_state["authed"] = True
        st.rerun()
    elif _entered:
        st.error("Wrong password.")
    st.stop()
st.title("🧪 QA Test Plan Generator")

ai_ok, ai_note = ai_available()
with st.sidebar:
    st.subheader("Generator")
    mode = st.radio("How to write test cases", ["AI (realistic)", "Rules only (offline)"],
                    index=0 if ai_ok else 1, disabled=not ai_ok,
                    help="AI mode writes ticket-specific steps and test data. Rules mode never sends data anywhere.")
    st.caption(ai_note)


def build_plan(ticket: dict) -> dict:
    rule_plan = generate_test_plan(ticket)
    if mode.startswith("AI") and ai_ok:
        try:
            return generate_ai_plan(ticket, rule_plan)
        except Exception as e:
            st.warning(f"AI generation failed, showing the rule-based plan instead. {e}")
    return rule_plan


tab_jira, tab_paste = st.tabs(["From Jira", "Paste ticket text"])

with tab_jira:
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    key_input = c1.text_input("Jira ticket key", placeholder="e.g. FL-7641")
    if c2.button("Generate test plan", type="primary", width="stretch"):
        if not re.fullmatch(r"\s*[A-Za-z][A-Za-z0-9_]+-\d+\s*", key_input or ""):
            st.warning("Enter a ticket key like FL-7641.")
        else:
            with st.spinner("Fetching ticket and writing test cases… local AI models can take a few minutes."):
                try:
                    ticket = fetch_ticket(key_input)
                    st.session_state.update(ticket=ticket, plan=build_plan(ticket))
                except Exception as e:
                    st.error(f"Could not load {key_input.strip().upper()}: {e}")

with tab_paste:
    st.write("Use this to try the generator without Jira, or for tickets you can't reach.")
    p_summary = st.text_input("Summary", key="p_summary")
    p_type = st.selectbox("Issue type", ["Story", "Bug", "Task"], key="p_type")
    p_desc = st.text_area("Description / acceptance criteria", height=220, key="p_desc")
    if st.button("Generate from text", width="stretch"):
        ticket = {"key": "MANUAL", "summary": p_summary or "Untitled", "description": p_desc,
                  "issue_type": p_type, "components": [], "labels": [], "priority": "", "status": ""}
        with st.spinner("Writing test cases… local AI models can take a few minutes."):
            st.session_state.update(ticket=ticket, plan=build_plan(ticket))

if "plan" not in st.session_state:
    st.stop()

plan, ticket = st.session_state["plan"], st.session_state["ticket"]
key = ticket["key"]

st.divider()
st.header(f"📋 {key}: {ticket['summary']}")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Testability score", f"{plan['testability_score']}/100",
          help="How clear the ticket is: AC section present, outcomes stated, no vague wording.")
m2.metric("Acceptance criteria", len(plan["criteria"]))
m3.metric("Test cases", len(plan["test_cases"]))
m4.metric("Written by", plan.get("generator", "Rules"))

with st.expander("Ticket details"):
    meta = [f"**Type:** {ticket.get('issue_type') or '–'}", f"**Status:** {ticket.get('status') or '–'}", f"**Priority:** {ticket.get('priority') or '–'}"]
    if ticket.get("components"):
        meta.append(f"**Components:** {', '.join(ticket['components'])}")
    if ticket.get("labels"):
        meta.append(f"**Labels:** {', '.join(ticket['labels'])}")
    st.markdown(" | ".join(meta))
    st.text_area("Description", ticket["description"] or "(empty)", height=200, disabled=True)

if plan["risks"]:
    st.subheader("⚠️ Open questions for the ticket owner")
    for r in plan["risks"]:
        st.markdown(f"- {r}")

st.subheader("🎯 Acceptance criteria and coverage")
if plan["criteria"]:
    st.dataframe(
        [{"AC": c["id"], "Criterion": c["text"], "Parsed from": c["source"],
          "Covered by": ", ".join(plan["traceability"].get(c["id"], [])) or "⚠️ none"}
         for c in plan["criteria"]],
        hide_index=True, width="stretch")
else:
    st.info("No acceptance criteria could be found in this ticket.")

s1, s2 = st.columns(2)
with s1:
    st.subheader("✅ In scope")
    for item in plan["scope_in"]:
        st.markdown(f"- {item}")
with s2:
    st.subheader("❌ Out of scope")
    for item in plan["scope_out"]:
        st.markdown(f"- {item}")

st.subheader("🧪 Test cases")
all_types = sorted({tc["type"] for tc in plan["test_cases"]})
chosen = st.multiselect("Show and export these types", all_types, default=all_types)
cases = [tc for tc in plan["test_cases"] if tc["type"] in chosen]

badge = {"Positive": "🟢", "Negative": "🔴", "Boundary": "🟠", "Edge Case": "🟡", "Security": "🔒",
         "Regression": "🔁", "Performance": "⏱️", "UI": "🖥️"}
for tc in cases:
    label = f"{badge.get(tc['type'], '⚪')} [{tc['id']}] {tc['title']} · {tc['type']} · {tc['priority']}"
    with st.expander(label):
        st.markdown(f"**Covers:** {', '.join(tc['covers']) or '–'}  \n**Why this test:** {tc['basis']}")
        st.markdown(f"**Preconditions:** {tc['preconditions']}")
        if tc.get("test_data"):
            st.markdown(f"**Test data:** {tc['test_data']}")
        st.markdown("**Steps:**\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(tc["steps"], 1)))
        st.markdown(f"**Expected result:** {tc['expected_result']}")

st.divider()
d1, d2, d3 = st.columns(3)
d1.download_button("📥 Download Markdown", to_markdown(plan, key, cases),
                   file_name=f"{key}_test_plan.md", mime="text/markdown", width="stretch")
d2.download_button("📥 Download CSV (Xray/Zephyr/TestRail)", to_csv(cases),
                   file_name=f"{key}_test_cases.csv", mime="text/csv", width="stretch")
with d3:
    if key == "MANUAL":
        st.button("💬 Post to Jira", disabled=True, width="stretch", help="Only for tickets loaded from Jira.")
    elif st.button("💬 Post to Jira as comment", width="stretch"):
        try:
            post_comment_to_jira(key, to_jira_wiki(plan, key, cases))
            st.success(f"Posted to {key}.")
        except Exception as e:
            st.error(f"Could not post the comment: {e}")
