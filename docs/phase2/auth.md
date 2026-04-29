## AUTHENTICATION IMPLEMENTATION

**Goal**: Establish a Zero Trust "Identity-Aware Gateway" where every MCP tool call is cryptographically tied to a Microsoft 365 identity. Access is governed not just at the session level, but at the **individual tool and resource level** using Entra ID claims and Administrative Units.

### 1. Core Architectural Shift: The MCP Gateway
In 2026, the industry standard has moved from simple middleware to an **Identity-Aware MCP Gateway**. This layer sits between the LLM client (Claude, ChatGPT) and the tool logic.

* **Identity Provider**: Microsoft Entra ID (v2.0 tokens required).
* **Protocol**: OAuth 2.1 (pre-registration flow).
* **Context Propagation**: The Gateway extracts the Entra `id_token` or `access_token`, validates it, and injects a `UserContext` into the MCP `request.metadata` before the tool logic executes.

---

### 2. Microsoft Entra ID Configuration
To support M365 authentication for MCP, implement a **Dual-App Registration** strategy to separate the *Resource Server* from the *Client*.

#### A. MCP Server Registration (The Resource)
* **Manifest**: Set `requestedAccessTokenVersion: 2`.
* **Expose an API**: Set the App ID URI to `api://{client_id}` and define a scope named `MCP.Invoke` under it.
* **App Roles**: Create roles (e.g., `Tool.Admin`, `Tool.Viewer`) for RBAC.

#### B. Client Registration (The AI Agent)
* **Redirect URIs**: 
    * `https://{domain}/auth/callback` (for Web/Mock UI).
    * `mcp://auth` (if using desktop-native MCP clients).
* **API Permissions**: Grant `User.Read` and delegated permission to `MCP.Invoke`.
* **Pre-authorization**: Add the Client IDs of authorized LLM interfaces (like the ChatGPT Enterprise ID or Claude's internal ID) to prevent "Consent Fatigue."

---

### 3. Detailed Authorization Model (ABAC/RBAC)
Utilize **Attribute-Based Access Control (ABAC)** using claims within the JWT.

| Component | Logic | Source |
| :--- | :--- | :--- |
| **Authentication** | JWT Signature + Audience + Expiry check. | Entra ID Header |
| **Identity Gating** | User must belong to the `HTH-MCP` security group. | `groups` claim |
<!-- Department-based gating is temporarily disabled.
| **Department Gating** | `departmentId` in the request must match the user's `officeLocation` or `extension_department` claim. | `UserContext` |
-->
| **Tool Gating** | Tool metadata `required_roles` must match the user's `roles` claim. | Tool Registry |

> [!IMPORTANT]
> **Least Privilege Binding**: Even if a user is authenticated, the MCP server should only initialize tools they are permitted to see. The `list_tools` capability must be filtered dynamically based on the token's claims.

---

### 4. Implementation Detail: Tool Gating in FastAPI
Using the 2026 `FastMCP` decorators, to bind authorization directly to the tool definition.

```python
# Example of Tool-Level Gating in app/tool/registry.py
from fastmcp import FastMCP, UserContext

mcp = FastMCP("M365_Tool_Gateway")

@mcp.tool(
    name="search_catalogue",
    required_claims={"department": "Finance", "role": "Manager"}
)
async def search_catalogue(query: str, ctx: UserContext):
    # ctx is automatically populated by the AuthMiddleware
    dept_id = ctx.claims.get("extension_departmentId")
    
    if not dept_id:
        raise HTTPException(status_code=403, detail="Department claim missing")
        
    return await db.query(query, filter_dept=dept_id)
```

---

### 5. Integration with GPT & Claude Clients
LLM clients handle authentication in two primary ways in 2026:

1.  **Static Header (Desktop)**: For local testing or CLI tools (like Claude Desktop), use an environment variable to pass a Long-Lived Token or use `claude mcp add --header "Authorization: Bearer <TOKEN>"`.
2.  **Dynamic OAuth (Web/Managed)**: When using the web-based Mock UI or ChatGPT/Claude Enterprise, the client initiates the OAuth flow.
    * The server detects a missing token and returns a **401 WWW-Authenticate: MCP-OAuth**.
    * The client displays a "Sign in with Microsoft" button.
    * The resulting token is stored in the client’s secure vault and sent in the `Authorization` header of every subsequent SSE (Server-Sent Events) or stdio message.

### MCP Connector Credentials vs Entra Credentials
There are two different credential pairs in this system:

1. **MCP connector credentials**
   - These are the `OAuth Client ID` and optional `OAuth Client Secret` that ChatGPT or Claude asks for in the connector screen.
   - They belong to the MCP bridge in this repo, not to Microsoft Entra.
   - You can obtain them by calling `POST /oauth/register` on the deployed MCP server, or you can pre-seed them with `HTH_MCP_OAUTH_CLIENT_ID` and `HTH_MCP_OAUTH_CLIENT_SECRET`.
   - `HTH_MCP_OAUTH_CLIENT_SECRET` is optional; if you leave it blank, the connector behaves like a public client and relies on authorization code + PKCE.
2. **Entra resource-app credentials**
   - These are the values used by the server to validate Microsoft 365 identity tokens.
   - They live in `HTH_AUTH_ISSUER`, `HTH_AUTH_JWKS_URL`, and `HTH_AUTH_AUDIENCE`.
   - `HTH_AUTH_AUDIENCE` must be the App ID URI, such as `api://<resource-client-id>`, not the scope name.

---

### 6. Updated Implementation Checklist

| Status | Task | Responsibility | Technical Requirement |
| :---: | :--- | :--- | :--- |
| ☐ | **Entra ID App Setup** | Security | Create Resource + Client Apps; configure Scopes. |
| ☐ | **Token Validator** | Backend | Implement `PyJWT` or `msal` logic for v2.0 tokens. |
| ☐ | **UserContext Middleware** | Backend | Inject `UserContext` into MCP request metadata. |
| ☐ | **Dynamic Tool Filtering** | Registry | Modify `list_tools` to check `UserContext` before responding. |
| ☐ | **Redis Session Keying** | DevOps | Key: `mcp:session:{tenant_id}:{user_id}`. |
| ☐ | **Audit Sidecar** | Observability | Log `(User, Tool, Success/Fail, Department)` to Sentinel/ELK. |
| ☐ | **Mock UI Bridge** | Frontend | Implement MSAL.js for browser-side token acquisition. |

---

### 7. Security Guardrails & Fail-Safe Logic
* **Fail-Closed Policy**: If the `departmentId` claim is missing or the token is "Thin" (missing required claims), the server must return a `403 Forbidden` and a structured error object indicating the missing claim.
* **Token Refresh Handling**: For long-running LLM sessions, the MCP server should return a specific `error.code: -32001 (Unauthorized)` which triggers the LLM client to refresh the token without losing the conversation context.
* **Rate Limiting by Identity**: Gate the number of tool calls per user/minute (e.g., 50 calls/min) to prevent "Agentic Loops" from exhausting API quotas.

---

## ADMIN CONFIGURATION

### Request Checklist
1. Create **two App Registrations** in Microsoft Entra ID.
2. Create a **Resource app** for the MCP service itself.
3. Create a **Client app** for the people or apps that will connect to MCP.
4. On the Resource app, expose a scope named **`MCP.Invoke`**.
5. Set the Resource app to use **Access token version 2**.
6. Add redirect URIs to the Client app for both use cases:
   - Web sign-in: `https://<your-domain>/auth/callback`
   - Desktop or local tools: `mcp://auth`
7. Pre-authorize trusted client applications so users are not prompted for consent again and again.
8. Confirm the people who need access are in the correct Microsoft 365 group.
9. Confirm the app roles are available:
   - `Tool.Viewer` for regular users
   - `Tool.Admin` for people who need to manage sessions

### Configuration Guide

The following settings tell the app how to recognize Microsoft Entra sign-in tokens. These are usually entered in Azure as application settings or environment variables.

| Setting | Definition| Configuration |
| :--- | :--- | :--- |
| `HTH_IDENTITY_AUTH_ENABLED=true` | Turns on Microsoft identity checks. If this is off, the app will not enforce Entra-based access rules. | Set this in your Azure app settings or deployment environment. |
| `HTH_AUTH_ISSUER=https://login.microsoftonline.com/tenant-id/v2.0` | This is the security address that tells the app who issued the sign-in token. | Use your Entra tenant’s login URL. |
| `HTH_AUTH_JWKS_URL=https://login.microsoftonline.com/tenant-id/discovery/v2.0/keys` | This is the public key address the app uses to verify the token is real and was not changed. | Use the JWKS URL for your Entra tenant. |
<!-- Root Cause vs Logic: Entra access tokens validate the API App ID URI in the `aud` claim, while the scope name lives in `scp`; keep `/MCP.Invoke` out of HTH_AUTH_AUDIENCE so real tokens pass audience validation. -->
| `HTH_AUTH_AUDIENCE=api://resource-client-id` | This is the specific service identifier the token is meant for. It helps prevent tokens for other apps from being accepted. | Use the Resource app's App ID URI, not the scope name. |
| `HTH_AUTH_REQUIRED_GROUP=HTH-MCP` | This is the Microsoft 365 group the user must belong to before they can use MCP. | Create or reuse a group with this name. |
| `HTH_AUTH_REQUIRED_CLAIMS=tid,oid` | These are the minimum identity details the app expects to see in the token. `tid` identifies the tenant and `oid` identifies the user. | Usually included automatically by Entra. |
<!-- Department-related claims are temporarily disabled.
| `HTH_AUTH_DEPARTMENT_CLAIMS=extension_departmentId,extension_department,officeLocation` | These are the department-related details the app can read from the token when it needs to check department-based access. | Your IT team may add these as optional claims or extension attributes. |
-->
| `HTH_AUTH_REQUIRED_TOKEN_VERSION=2.0` | Tells the app to expect a modern Entra token format. | Set to `2.0` for production. |
| `HTH_MCP_OAUTH_CLIENT_ID=<client-id>` | Seeds a reusable MCP connector client registration for ChatGPT/Claude manual setup. | Optional. Use the value returned by `POST /oauth/register`, or set your own if you want a stable client ID. |
| `HTH_MCP_OAUTH_CLIENT_SECRET=<client-secret>` | Optional secret for the seeded MCP connector client. | Leave blank for public-client + PKCE setups; set it when the client uses `client_secret_post`. |


Useful rule of thumb:
1. `Issuer` and `JWKS URL` are the token’s security addresses.
2. `Audience` is the exact service name the token must match.
3. `Enabled` turns the whole identity check on or off.

### Managing Access via Microsoft 365
Access is controlled in two layers: who can sign in, and what they are allowed to do after they sign in.

#### Step 1: Create or use the `HTH-MCP` group
1. Open the Microsoft 365 admin or Entra admin portal.
2. Create a security group named `HTH-MCP`, or use an existing group if your organization already has one for this purpose.
3. Add the people who should be allowed to use the MCP service.
4. Keep this group small and intentional. If someone leaves the project, remove them from the group.

#### Step 2: Understand the role names
Roles are simple labels that describe what a person can do:
1. `Tool.Viewer` means the person can use the normal tools.
2. `Tool.Admin` means the person can also manage or clear sessions when needed.
Most users should only get `Tool.Viewer`. Give `Tool.Admin` only to the few people who truly need it.

#### Step 3: Match access to the right people
1. Put regular project users in `HTH-MCP` and give them `Tool.Viewer`.
2. Put support or operations staff in `HTH-MCP` and give them `Tool.Admin` if they need session control.
3. Do not assign admin access broadly. Keep it limited to the smallest practical group.

### Data and Privacy (Claims)
The app needs a few pieces of information from the sign-in token to make correct access decisions.
1. `tid` tells the app which Microsoft tenant the user belongs to.
2. `oid` tells the app which user is signed in.
<!-- Department-related values such as `extension_departmentId`, `extension_department`, or `officeLocation` are temporarily disabled.
3. Department-related values such as `extension_departmentId`, `extension_department`, or `officeLocation` help the app decide whether a request belongs to the right department.
-->
> Think of these as labels inside the digital key that comes with the sign-in. If the labels are missing, the app cannot safely confirm access and will fail closed.

Important guidance:
1. Make sure these claims are included in the token the Entra setup sends to the app.
2. Keep the data limited to what the app actually needs.
<!-- Department-based access is temporarily disabled.
3. If your organization uses department-based access, confirm the department values are accurate before rollout.
-->

### Production Safety Rules
1. **Remove local testing secrets before production.**
   - Do not use `HTH_AUTH_JWT_HS256_SECRET` in production.
   - That setting is only for local tests or development checks.
2. **Use token verification from Entra in live environments.**
   - Production should rely on the official Entra issuer and JWKS settings, not a shared test secret.
3. **Keep rate limits in place.**
   - `HTH_AUTH_RATE_LIMIT_PER_MINUTE` prevents one user or one bot from overwhelming the service with repeated calls.
   - This protects the app, reduces accidental loops, and helps keep the experience stable for everyone else.
4. **Treat missing claims as a security issue, not a warning.**
   - If the token does not include the required information, the app should deny access instead of guessing.

### Quick Setup Flow
1. Create the Resource app and Client app.
2. Expose `MCP.Invoke` and set access token version 2.
3. Add the redirect URIs for web and desktop use.
4. Create the `HTH-MCP` group and add the right people.
5. Confirm the token contains the required claims.
6. Set the `HTH_AUTH_*` environment variables in Azure.
7. Turn on `HTH_IDENTITY_AUTH_ENABLED=true`.
8. Remove any local-only HS256 secret before production go-live.
9. Test sign-in, tool visibility, and one normal tool call before opening access to the full team.
