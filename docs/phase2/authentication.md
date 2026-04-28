## Phase 2 Authentication & Gatekeeping Strategy

**Goal**: Plan a secure login and authorization experience so that every interaction (via ChatGPT, Claude, or any compliant desktop/browser client) is tied to a known Microsoft 365 user and that only permitted tools/departments surface inside the mock UI or API responses.

### Requirements

- Authenticate every incoming request via Microsoft 365 (Entra ID) using OAuth/OIDC so that the runtime never processes unauthenticated traffic.
- Ensure GPT/Claude clients present a valid Microsoft 365 token before they can hit `POST /api/v1/query`, `GET /api/v1/mock-ui`, or any tool endpoints.
- Authorize access per user: determine which tools (or `departmentId`s) the user may invoke, based on centralized claims/groups.
- Log every authentication & authorization decision for auditability and support debugging/gating decisions.
- Fail safely: when tokens are missing/expired or authorization fails, return clear HTTP errors (401/403) plus guidance the client can surface.

### High-level Architecture

1. **Identity provider**: Microsoft 365 (Entra ID) issues ID/access tokens after the user signs in via their company-bound identity. Tokens carry claims for user ID, email, roles, group memberships, and custom extensions (e.g., `departmentIdAllowed`).
2. **Client setup**: Document how GPT/Claude desktop/browser can be configured to automatically acquire tokens—either via built-in Microsoft Entra integration or by using the native browser session. The token gets routed in an `Authorization: Bearer <token>` header when the client hits our endpoints.
3. **API gateway/middleware**: Every `FastAPI` route behind `app/main.py` should run the same auth guard:
   - Validate signature/issuer/audience.
   - Ensure token is current (check `exp`) and optionally perform token introspection against Entra if revocation is required.
   - Enrich the request context with a structured `UserContext` object (id/email/groups).
4. **Tool registry filter**: Before invoking a tool (e.g., `stock.search_catalogue`), the orchestrator consults the `UserContext` and a configurable `AuthorizationPolicy` map to decide if the caller is allowed. This map can be seeded from Azure AD group claims or from a company-managed ACL service.
5. **Department gating**: We treat `departmentId` as a capability scope. When a user wants product information for `departmentId=3`, we verify the token's `departmentIds` claim includes that ID or that the policy explicitly allows it.
6. **Session state**: Ensure session data stored in Redis is keyed by the authenticated user (e.g., prefixed by `user:{user_id}:session`). This prevents one user from loading another's state when tokens rotate or when the GPT mock UI passes a stale `sessionId`.

### Authorization Model & Best Practices

- **Least privilege**: Default to denying access until a claim or policy explicitly grants it.
- **Claim-driven policies**: Prefer passing `roles`/`groups`/`departmentIds` claims from Entra to drive policies so you avoid a separate user database. Only fallback to a custom store when claims are insufficient.
- **Tool metadata**: Each tool definition should declare required roles/claims. During registration (`app/tool/registry.py`), attach a `required_claims` structure that the guard can check quickly.
- **Caching policies**: Cache policy decisions for short TTLs (e.g., 5 minutes) using an in-memory cache or Redis so repeated checks don’t hit Microsoft Entra constantly.
- **Fail-open vs fail-closed**: Always fail-closed for authorization. If the policy cache is stale or Entra is temporarily unreachable, return 403 rather than elevating privilege.
- **Tool-level auditing**: Emit structured logs whenever a tool call is dropped due to insufficient authorization so operators can adjust policies quickly.
- **Separation of concerns**: Keep auth middleware independent from business logic; expose hooks so the orchestrator can query `request.state.user_context` safely.
- **Self-service provisioning**: Use Microsoft 365 group membership to onboard users—granting a user access to particular groups automatically unlocks the mapped tools/department IDs without redeploys.
- **Re-check on tool invocation**: Even if streaming responses are authorized once, revalidate before each tool call since the user might pass a new department ID as part of a later request.

### Integration with GPT/Claude Clients

1. **Guided login**: Document how the mock UI or a simple `/auth/login` endpoint can redirect users into the Microsoft Entra login flow (device code, QR, or direct browser) that prepopulates GPT/Claude in-app browsers.
2. **Token refresh**: Provide instructions for refreshing tokens; ideally store `refresh_token` securely server-side or instruct clients to reauthenticate when 401 is received.
3. **Scoped tokens**: Request tokens that include custom claims (e.g., `departmentIdAllowed`) from Entra by configuring optional claims or app roles in the Azure portal. Use these claims to short-circuit authorization decisions.
4. **Trusted metadata endpoint**: Create an internal `GET /api/v1/auth/me` route that returns the user context (with permitted tools/departments) after validating the current token. GPT/Claude clients can call this right after login to fetch their entitlement list.

### Planning Checklist

| Task | Owner | Notes |
| --- | --- | --- |
| Define Microsoft 365 app registration details (client ID, secrets, redirect URIs for GPT/Claude embedded browsers). | Security/Platform team | Document required scopes (e.g., `openid profile email Api.Access`). |
| Implement `AuthMiddleware` that validates tokens and builds `UserContext`. | Backend eng | Reuse existing JWT utilities if present. Add test coverage for invalid tokens. |
| Extend tool registry to declare per-tool `required_claims`. | Backend eng | Consider adding `department_scope` metadata to tools that call Harmonise. |
| Build `AuthorizationPolicy` service that maps users/groups → tools/department IDs. | Backend eng | Pluggable driver (Azure AD graph vs YAML config). |
| Document GPT/Claude client configuration steps for company accounts. | Documentation owner | Include screenshots or step-by-step for common clients. |
| Create `/api/v1/auth/me` endpoint and mock UI hook so clients can see their permissions. | API team | Reuse same `UserContext` guard. |
| Audit logging strategy for auth + tool gate failures. | Observability team | Send to existing logging pipeline (structured JSON). |
| Refresh/expiration handling (handle `exp` + `nbf`, configure clock skew). | DevOps | Document expected skew and rotation cadence. |
| Error handling guidance for clients (401 vs 403 messages, reauth instructions). | UX/Docs | Ensure inline errors describe next steps for GPT/Claude flows. |

### Future Considerations

- **Delegated admin console**: Provide an internal dashboard to view which users or groups are allowed to access each tool/department, syncing with Microsoft 365 groups daily.
- **Dynamic policy updates**: Listen for webhook notifications from Entra when group membership changes so cached policy decisions can expire immediately.
- **Rate limiting**: Gate queries at both token and department level to prevent abuse once authenticated.
- **Granular session recording**: Tie session/log IDs back to authenticated users, making it easier to correlate queries with Microsoft 365 identities in incident response.

This document should serve as the source of truth when we later build the authentication/authorization layer, ensuring every GPT/Claude user flows through the same secure gate controlled by Microsoft 365 claims.
