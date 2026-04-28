## Phase 2: Authentication & Gatekeeping Strategy

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
* **Expose an API**: Define a scope `api://{client_id}/MCP.Invoke`.
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
| **Identity Gating** | User must belong to the `MCP_Users` security group. | `groups` claim |
| **Department Gating** | `departmentId` in the request must match the user's `officeLocation` or `extension_department` claim. | `UserContext` |
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
