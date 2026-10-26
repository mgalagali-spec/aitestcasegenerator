Step 1: Create the Project Folder & Virtual Environment
Open PowerShell on your Windows machine and run:

PowerShell
mkdir jira-qa-agent
cd jira-qa-agent
python -m venv venv
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\venv\Scripts\Activate.ps1



Step 2: Install Required Packages
Install the three required packages into your virtual environment:

PowerShell
.\venv\Scripts\python.exe -m pip install streamlit jira python-dotenv

Step 3: Get Your Atlassian Jira API Token
Go to https://id.atlassian.com/manage-profile/security/api-tokens.

Click Create API token.

Label it (e.g., local-qa-agent) and click Create.

Copy the generated token (it usually starts with ATATT...).

Step 4: Create the .env Configuration File
In your jira-qa-agent folder, create a file named .env:

Code snippet
JIRA_URL=https://smartshift.atlassian.net
JIRA_EMAIL=mgalagali@smartshift.com
JIRA_API_TOKEN=your_actual_atlassian_token_here
Replace your_actual_atlassian_token_here with the token copied in Step 3.

Do not add spaces around the = signs.

Do not put quotes around the values.

Step 5: Create the Application File (app.py)
Create a file named app.py in your jira-qa-agent folder and paste this complete code:

import os
import re
import urllib3
import streamlit as st
from dotenv import load_dotenv
from jira import JIRA

# 1. Suppress corporate proxy SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

# --- Jira Connection ---
def get_jira_client():
    jira_url = os.getenv("JIRA_URL")
    jira_email = os.getenv("JIRA_EMAIL")
    jira_token = os.getenv("JIRA_API_TOKEN")

    if not (jira_url and jira_email and jira_token):
        raise ValueError("Missing Jira credentials in .env file!")

    return JIRA(
        server=jira_url,
        basic_auth=(jira_email, jira_token),
        options={"verify": False}  # Bypasses corporate network SSL inspection
    )

def fetch_ticket(ticket_key: str):
    jira = get_jira_client()
    issue = jira.issue(ticket_key.strip())
    return {
        "key": issue.key,
        "summary": issue.fields.summary,
        "description": issue.fields.description or "No description provided.",
        "issue_type": issue.fields.issuetype.name,
    }

def post_comment_to_jira(ticket_key: str, comment_text: str):
    jira = get_jira_client()
    jira.add_comment(ticket_key, comment_text)

# --- Rule-Based Test Plan Engine ---
def parse_acceptance_criteria(description: str):
    criteria = []
    lines = description.splitlines()
    for line in lines:
        cleaned = line.strip()
        if re.match(r"^(\*|-|\d+\.|\[\s*\]|\* \[[ xX]\])\s+", cleaned):
            text = re.sub(r"^(\*|-|\d+\.|\[\s*\]|\* \[[ xX]\])\s+", "", cleaned).strip()
            if len(text) > 4:
                criteria.append(text)
    
    if not criteria:
        sentences = [s.strip() for s in re.split(r"[.\n]", description) if len(s.strip()) > 10]
        criteria = sentences[:4]
        
    return criteria if criteria else ["Verify the primary functional workflow described in the ticket."]

def generate_rule_based_test_plan(ticket: dict):
    summary = ticket["summary"]
    desc = ticket["description"]
    criteria = parse_acceptance_criteria(desc)
    
    test_cases = []
    tc_counter = 1

    # 1. Functional Happy Path
    for crit in criteria:
        test_cases.append({
            "id": f"TC-{tc_counter:03d}",
            "title": f"Verify successful execution: {crit[:60]}...",
            "type": "Positive",
            "preconditions": "User is authenticated with active permissions and valid test data.",
            "steps": [
                f"Navigate to the target feature area for '{summary}'.",
                f"Perform the action defined by criterion: '{crit}'.",
                "Submit or trigger the execution."
            ],
            "expected_result": f"System processes the action successfully meeting criteria: '{crit}'."
        })
        tc_counter += 1

    # 2. Input Validation / Boundary Value
    test_cases.append({
        "id": f"TC-{tc_counter:03d}",
        "title": "Validate handling of empty, null, and boundary values",
        "type": "Negative",
        "preconditions": "Form / input interface is accessible.",
        "steps": [
            "Attempt submission leaving all required fields blank.",
            "Input boundary strings (max length, special characters, unicode, SQL strings).",
            "Click Submit / Save."
        ],
        "expected_result": "System raises clear inline validation errors; invalid payloads are rejected."
    })
    tc_counter += 1

    # 3. Security / Access Control
    test_cases.append({
        "id": f"TC-{tc_counter:03d}",
        "title": "Verify access control and unauthorized user denial",
        "type": "Security",
        "preconditions": "An account without privileges to perform this operation is available.",
        "steps": [
            "Log in with a restricted or read-only role.",
            f"Attempt to access or invoke '{summary}'.",
            "Attempt direct URL navigation or API execution."
        ],
        "expected_result": "Access is blocked with 403 Forbidden or redirected to unauthorized landing page."
    })
    tc_counter += 1

    # 4. Error Recovery / Resilience
    test_cases.append({
        "id": f"TC-{tc_counter:03d}",
        "title": "Verify recovery during network latency or timeout",
        "type": "Edge Case",
        "preconditions": "Network throttling / browser offline mode enabled in dev tools.",
        "steps": [
            f"Trigger the primary action for '{summary}'.",
            "Simulate connection drop or timeout mid-flight.",
            "Restore connection."
        ],
        "expected_result": "UI shows a retry option; no duplicate records are created."
    })
    tc_counter += 1

    return {
        "summary": f"Functional, security, and edge-case validation for {ticket['key']}: {summary}",
        "scope_in": [
            f"Core acceptance criteria validation for '{summary}'",
            "Input boundary and validation failure handling",
            "Permission / authorization checks"
        ],
        "scope_out": [
            "High-volume load/performance endurance tests",
            "Third-party upstream service failovers"
        ],
        "risks": [
            "Missing edge-case definitions in acceptance criteria",
            "Potential data mismatch across environments"
        ],
        "test_cases": test_cases
    }

# --- Streamlit UI ---
st.set_page_config(page_title="Local QA Test Plan Generator", page_icon="🧪", layout="wide")

st.title("🧪 Local QA Test Plan Generator")
st.caption("Pure Python • Zero External AI Dependencies • Offline Rule Engine")

col1, col2 = st.columns([3, 1])
with col1:
    ticket_key_input = st.text_input("Enter Jira Ticket Key:", placeholder="e.g. FL-7641")
with col2:
    st.write("")
    st.write("")
    fetch_btn = st.button("Generate Test Plan", type="primary", use_container_width=True)

if fetch_btn:
    if not ticket_key_input:
        st.warning("Please enter a valid Jira ticket key!")
    else:
        with st.spinner("Fetching ticket from Jira and building test cases..."):
            try:
                ticket = fetch_ticket(ticket_key_input)
                
                with st.expander(f"📌 Ticket Details: {ticket['key']} - {ticket['summary']}", expanded=False):
                    st.write(f"**Type:** {ticket['issue_type']}")
                    st.text_area("Description / Acceptance Criteria", ticket["description"], height=120, disabled=True)

                plan = generate_rule_based_test_plan(ticket)
                st.session_state["plan"] = plan
                st.session_state["ticket_key"] = ticket["key"]
                st.success("Test Plan Generated Successfully!")

            except Exception as e:
                st.error(f"Error: {e}")

if "plan" in st.session_state:
    plan = st.session_state["plan"]
    ticket_key = st.session_state["ticket_key"]

    st.divider()
    st.header(f"📋 Test Plan for {ticket_key}")
    st.write(f"**Summary:** {plan['summary']}")

    col_scope1, col_scope2 = st.columns(2)
    with col_scope1:
        st.subheader("✅ In Scope")
        for item in plan["scope_in"]:
            st.markdown(f"- {item}")

    with col_scope2:
        st.subheader("❌ Out of Scope")
        for item in plan["scope_out"]:
            st.markdown(f"- {item}")

    st.subheader("⚠️ Risks & Considerations")
    for r in plan["risks"]:
        st.markdown(f"- {r}")

    st.subheader("🧪 Test Cases")
    
    markdown_export = [f"# Test Plan for {ticket_key}\n\n**Overview:** {plan['summary']}\n"]
    markdown_export.append("## Test Cases\n")

    for tc in plan["test_cases"]:
        badge = {
            "Positive": "🟢",
            "Negative": "🔴",
            "Edge Case": "🟡",
            "Security": "🔒"
        }.get(tc["type"], "⚪")

        with st.expander(f"{badge} **[{tc['id']}] {tc['title']}** ({tc['type']})"):
            st.markdown(f"**Preconditions:** {tc['preconditions']}")
            st.markdown("**Steps:**")
            for idx, step in enumerate(tc["steps"], 1):
                st.markdown(f"{idx}. {step}")
            st.markdown(f"**Expected Result:** `{tc['expected_result']}`")

        markdown_export.append(f"### [{tc['id']}] {tc['title']} ({tc['type']})")
        markdown_export.append(f"- **Preconditions:** {tc['preconditions']}")
        markdown_export.append("- **Steps:**")
        for idx, s in enumerate(tc["steps"], 1):
            markdown_export.append(f"  {idx}. {s}")
        markdown_export.append(f"- **Expected Result:** {tc['expected_result']}\n")

    full_markdown = "\n".join(markdown_export)

    st.divider()
    col_d1, col_d2 = st.columns(2)
    
    with col_d1:
        st.download_button(
            label="📥 Download Test Plan (Markdown)",
            data=full_markdown,
            file_name=f"{ticket_key}_test_plan.md",
            mime="text/markdown",
            use_container_width=True
        )

    with col_d2:
        if st.button("💬 Post Test Plan to Jira Ticket as Comment", use_container_width=True):
            try:
                post_comment_to_jira(ticket_key, full_markdown)
                st.success("Successfully posted as comment to Jira!")
            except Exception as e:
                st.error(f"Failed to post comment: {e}")
				
				
Step 6: Launch Your App
In your terminal, execute:

PowerShell
.\venv\Scripts\streamlit.exe run app.py



Your browser will automatically open:

Plaintext
http://localhost:8501			