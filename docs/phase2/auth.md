## High-level Architecture

[cite_start]The following table outlines the interaction flow between Claude, the Identity Provider (IdP), the custom MCP server, and downstream services[cite: 7, 8]:

| Flow | Component | Description |
| :--- | :--- | :--- |
| 1 | Claude (client) | The starting point for the request. |
| 2 | | |
| 3 | | OAuth 2.0 (Authorization Code flow) |
| 4 | | |
| 5 | Azure AD (or other OAuth IdP) | Authenticates the user and issues tokens. |
| 6 | | |
| 7 | | Access Token (JWT) |
| 8 | | |
| 9 | Custom MCP Server (Python) | Hosted on Azure App Service; processes requests. |
| 10 | | |
| 11 | | Tool execution |
| 12 | | |
| 13 | Downstream services / APIs | Final destination for data processing or retrieval. |

---

## Step 1 – Register an OAuth application (Client ID & Secret)

[cite_start]Claude does not generate the OAuth client ID or secret; you must do this using an Identity Provider (IdP)[cite: 10].

### Option A (Recommended): Azure Entra ID (Azure AD)
1. [cite_start]Go to **Azure Portal → Entra ID → App registrations**[cite: 13].
2. [cite_start]Click **New registration**[cite: 14].
3. [cite_start]Set the following[cite: 15, 16, 17]:
   - **Name**: `claude-mcp-connector`
   - **Supported account types**: Single tenant (recommended)
4. [cite_start]Set the **Redirect URI**[cite: 18].
5. [cite_start]Click **Register**[cite: 19].
6. **✅ You now have**:
   - [cite_start]Application (client) ID [cite: 21]
   - [cite_start]Directory (tenant) ID [cite: 22]

### Create Client Secret
1. [cite_start]Inside the App Registration, go to **Certificates & secrets**[cite: 24].
2. [cite_start]Click **New client secret**[cite: 25].
3. [cite_start]**Copy the value immediately**[cite: 26].
4. [cite_start]**✅ This is your OAuth Client Secret**[cite: 27].

### Alternative IdPs
[cite_start]You can also use: Auth0, Okta, Keycloak, or Cognito[cite: 28, 29, 30, 31, 32, 33]. The concept remains the same:
- [cite_start]Client ID + Client Secret come from the IdP[cite: 35].
- [cite_start]Redirect URI must point to your MCP server[cite: 36].

---

## Step 2 – Store OAuth credentials in Azure App Service

> [cite_start]⚠️ **Warning**: Never hard-code secrets in Python code[cite: 38].

### Set Application Settings
[cite_start]Go to **Azure App Service → Configuration → Application settings** and add the following keys[cite: 39, 40, 41, 42]:

| Key | Value |
| :--- | :--- |
| `OAUTH_CLIENT_ID` | `<your-client-id>` |
| `OAUTH_CLIENT_SECRET` | `<your-client-secret>` |
| `OAUTH_TENANT_ID` | `<tenant-id>` |
| `OAUTH_AUTHORITY` | `https://login.microsoftonline.com/<tenant-id>` |

[cite_start]**Optional (typical settings)[cite: 43, 44]:**
- `OAUTH_AUDIENCE`: `api://<client-id>`
- `OAUTH_SCOPE`: `api://<client-id>/.default`

[cite_start]✅ These settings become environment variables available in your Python code[cite: 45].

---

## Step 3 – Implement OAuth endpoints in your MCP server (Python)

[cite_start]Your MCP server must expose specific OAuth endpoints for Claude to use[cite: 46, 47].

### [cite_start]Required Endpoints [cite: 48, 49]
| Endpoint | Purpose |
| :--- | :--- |
| `/oauth/login` | Starts OAuth flow |
| `/oauth/callback` | Receives authorization code |
| `/oauth/token/validate` | Validates access token |
| `MCP tool endpoints` | Require valid token |

### [cite_start]Load OAuth Environment Variables [cite: 50, 51]
```python
import os

CLIENT_ID = os.environ[\"OAUTH_CLIENT_ID\"]
CLIENT_SECRET = os.environ[\"OAUTH_CLIENT_SECRET\"]
TENANT_ID = os.environ[\"OAUTH_TENANT_ID\"]

AUTHORITY = f\"[https://login.microsoftonline.com/](https://login.microsoftonline.com/){TENANT_ID}\"
TOKEN_URL = f\"{AUTHORITY}/oauth2/v2.0/token\"
AUTHORIZE_URL = f\"{AUTHORITY}/oauth2/v2.0/authorize\"
```
[cite_start]✅ The OAuth Client ID and Secret are read directly from the Azure App Service configuration[cite: 52].

---

## Step 4 – OAuth login endpoint (`/oauth/login`)

[cite_start]Claude calls this endpoint to begin authentication[cite: 53, 54].

[cite_start]**Example (FastAPI)[cite: 55, 56]:**
```python
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
import urllib.parse

app = FastAPI()

@app.get(\"/oauth/login\")
def oauth_login():
    params = {
        \"client_id\": CLIENT_ID,
        \"response_type\": \"code\",
        \"redirect_uri\": \"https://<your-app>.azurewebsites.net/oauth/callback\",
        \"response_mode\": \"query\",
        \"scope\": \"openid profile email api://<client-id>/.default\",
        \"state\": \"claude\",
    }

    url = AUTHORIZE_URL + \"?\" + urllib.parse.urlencode(params)
    return RedirectResponse(url)
```

---

## Step 5 – OAuth callback endpoint (`/oauth/callback`)

[cite_start]This endpoint receives the authorization code, exchanges it for an access token, and returns success to Claude[cite: 57, 58, 59, 60, 61].

[cite_start]**Example Implementation[cite: 62]:**
```python
import requests

@app.get(\"/oauth/callback\")
def oauth_callback(code: str):
    data = {
        \"grant_type\": \"authorization_code\",
        \"client_id\": CLIENT_ID,
        \"client_secret\": CLIENT_SECRET,
        \"code\": code,
        \"redirect_uri\": \"https://<your-app>.azurewebsites.net/oauth/callback\",
        \"scope\": \"api://<client-id>/.default\",
    }

    token_resp = requests.post(TOKEN_URL, data=data).json()
    return token_resp
```
✅ **Security Note**: The Client Secret is used ONLY here (server-side). [cite_start]Never expose it to Claude or browsers[cite: 63].

---

## Step 6 – Token validation middleware

[cite_start]Claude sends the access token in the header[cite: 64, 65, 66]:
`Authorization: Bearer <access_token>`

[cite_start]Add middleware to validate the JSON Web Tokens (JWT)[cite: 67, 68]:
```python
from jose import jwt

def validate_token(token: str):
    payload = jwt.decode(
        token,
        key=PUBLIC_KEYS,
        audience=\"api://<client-id>\",
        issuer=AUTHORITY,
    )
    return payload
```
[cite_start]Note: You typically fetch Azure AD public keys from[cite: 69, 70]:
`https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys`

---

## Step 7 – Expose MCP endpoints secured by OAuth

[cite_start]Example of securing an MCP tool endpoint[cite: 71, 72, 73]:
```python
from fastapi import Depends, Header, HTTPException

def auth(authorization: str = Header(...)):
    token = authorization.replace(\"Bearer \", \"\")
    validate_token(token)

@app.post(\"/mcp/tools/search\", dependencies=[Depends(auth)])
def search_tool(query: dict):
    return {\"result\": \"secure data\"}
```

---

## Step 8 – Configure Claude Custom Connector (MCP)

[cite_start]In Claude (or Claude Enterprise config), use the following configuration[cite: 74, 75, 76]:

```json
{
  \"name\": \"azure-mcp\",
  \"transport\": {
    \"type\": \"http\",
    \"base_url\": \"https://<your-app>.azurewebsites.net\"
  },
  \"auth\": {
    \"type\": \"oauth2\",
    \"authorization_url\": \"https://<your-app>.azurewebsites.net/oauth/login\",
    \"token_validation_url\": \"https://<your-app>.azurewebsites.net/oauth/token/validate\"
  }
}
```
[cite_start]✅ Claude never stores your client secret; it only interacts via OAuth redirects and access tokens[cite: 77].

---

## Summary: Where Credentials Live

| Location | Client ID | Client Secret |
| :--- | :--- | :--- |
| Azure Entra ID | ✅ Issued | ✅ Issued |
| Azure App Service → App Settings | ✅ Stored | ✅ Stored |
| Python MCP code | ✅ Read from env | ✅ Read from env |
| Claude | ❌ No | ❌ No |

[cite_start][cite: 78, 79]

---

## [cite_start]Common Mistakes to Avoid [cite: 80, 81]

- ❌ Putting client secret in Claude configuration.
- ❌ Hard-coding secrets in Python code.
- ❌ Forgetting HTTPS (OAuth requires it).
- ❌ Missing redirect URI registration.
- ❌ Not validating JWT audience/issuer.