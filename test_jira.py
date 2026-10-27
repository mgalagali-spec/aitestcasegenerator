"""Quick connectivity check. Reads everything from .env – never put secrets in this file."""
import os
import sys

from dotenv import load_dotenv
from jira import JIRA

try:
    import truststore
    truststore.inject_into_ssl()   # use the Windows certificate store (corporate proxy friendly)
except ImportError:
    pass

load_dotenv()
url, email, token = os.getenv("JIRA_URL"), os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")
if not (url and email and token):
    sys.exit("Set JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN in .env")

jira = JIRA(server=url, basic_auth=(email, token))

user = jira.myself()
print(f"✅ Logged in as: {user.get('displayName')} ({user.get('emailAddress')})")

key = sys.argv[1] if len(sys.argv) > 1 else "FL-7641"
issue = jira.issue(key)
print(f"✅ Fetched {issue.key}: {issue.fields.summary} [{issue.fields.status.name}]")

ac = [f for f in jira.fields() if "acceptance" in f["name"].lower()]
print("Acceptance-criteria custom fields:", [(f["id"], f["name"]) for f in ac] or "none")
