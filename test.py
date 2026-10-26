import truststore
truststore.inject_into_ssl()  # Injects Windows cert store into all python requests

import os
import requests

for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    os.environ.pop(k, None)

try:
    res = requests.get("https://smartshift.atlassian.net", timeout=10)
    print("Connection status:", res.status_code)
    print("SUCCESS: Your machine can reach Jira with Windows certificates!")
except Exception as err:
    print("FAILED:", err)