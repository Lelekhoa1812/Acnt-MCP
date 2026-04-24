## 1. Architectural Overview
To allow the Claude.ai web interface to communicate with Harmonise, you must expose Harmonise as a **Remote MCP Server** using the **HTTP Transport layer**. Unlike local `stdio` setups used for desktop apps, this requires a stable, publicly routable endpoint.

| Component | Role |
| :--- | :--- |
| **Claude.ai** | The Host. It fetches the manifest and sends tool-call requests. |
| **Azure App Service** | The Gateway. Hosts the Harmonise MCP adapter. |
| **MCP Manifest** | The "Contract." A JSON file telling Claude what tools exist. |
| **Harmonise Engine** | The Backend. Executes the actual logic and returns data to Claude. |

---

## 2. Step-by-Step Deployment to Azure
We recommend **Azure App Service** (Linux) or **Azure Container Apps** for this, as they provide built-in TLS, easy environment variable management, and "Always On" capabilities to prevent Claude from timing out during cold starts.

### Phase A: Prepare the Harmonise MCP Adapter
Your Harmonise service needs a thin wrapper that speaks the MCP HTTP protocol.
1.  **Expose the Manifest:** Create an endpoint at `https://<your-app>.azurewebsites.net/.well-known/mcp-manifest.json`.
2.  **Implementation:** Ensure your server handles `POST /mcp` (or your designated endpoint) to receive JSON-RPC payloads from Claude.

### Phase B: Provisioning Azure Resources
1.  **Create a Web App:** Use the Azure CLI or Portal to create a new **App Service**.
2.  **Configure HTTPS:** Azure provides `*.azurewebsites.net` with managed TLS by default. Ensure "HTTPS Only" is toggled **ON**.
3.  **Environment Variables:** In the Azure Portal, go to **Settings > Configuration** and add your "MCP Keys" here:
    * `HARMONISE_API_KEY`: The internal key for your system.
    * `MCP_SHARED_SECRET`: A key you will use to validate that requests are actually coming from your authorized Claude session.

---

## 3. The MCP Manifest (The "Bridge")
Claude needs to know what tools are available. You must host a manifest file. This is the "Key" Claude uses to unlock your system.

```json
// Path: /.well-known/mcp-manifest.json
{
  "mcp_version": "2025-06-18",
  "name": "Harmonise-Production",
  "version": "1.2.0",
  "capabilities": {
    "tools": [
      {
        "name": "fetch_system_logs",
        "description": "Reads logs from Harmonise for troubleshooting.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "component": { "type": "string" },
            "lines": { "type": "number", "default": 50 }
          }
        }
      },
      {
        "name": "update_harmonise_config",
        "description": "Updates system parameters in real-time.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "key": { "type": "string" },
            "value": { "type": "string" }
          }
        }
      }
    ]
  },
  "endpoints": {
    "http": "https://harmonise-api.azurewebsites.net/mcp"
  }
}
```

---

## 4. Connecting to Claude.ai
Once your Azure service is live and returning the JSON manifest above, follow these steps in the Claude browser interface:

1.  **Open Claude Connect:** Go to your Claude settings (usually via the profile icon or the "Interactions" menu in 2026).
2.  **Add New MCP Integration:**
    * Select **"Add Remote MCP Server"**.
    * **URL:** Enter your manifest URL (e.g., `https://harmonise-api.azurewebsites.net/.well-known/mcp-manifest.json`).
3.  **Configure Headers (The Security Key):**
    * Claude will ask for any custom headers required for the connection.
    * Add: `Authorization: Bearer <Your_MCP_Shared_Secret>`.
4.  **Validate:** Claude will perform a "handshake" to fetch the tools. Once green, you can simply type: *"Hey Claude, check the Harmonise logs for the last 10 minutes"* and it will call your tool automatically.

---

## 5. Security and Validation
Since you are exposing your system to the web, security is paramount:

* **IP Whitelisting:** If possible, restrict traffic to your Azure App Service to only allow Anthropic’s IP ranges (check Anthropic’s 2026 documentation for their egress CIDR blocks).
* **Request Validation:** Your Harmonise adapter should check the `Authorization` header on every request.
* **Audit Logging:** Log every tool call Claude makes. This is essential for "Planning and Validating" (one of your requirements) to ensure Claude isn't hallucinating arguments for your system.

> [!IMPORTANT]
> **Key Management Note:** You do not need an "Anthropic API Key" inside Harmonise. Harmonise is the *provider*. Claude.ai uses *your* Harmonise key to authenticate into *your* system.

---

## Summary Checklist
- [ ] **Deploy Code:** Push your MCP-wrapped Harmonise to Azure.
- [ ] **Verify Manifest:** Ensure `/.well-known/mcp-manifest.json` is public.
- [ ] **Configure Claude:** Paste the URL into Claude.ai "Connect" settings.
- [ ] **Set Headers:** Ensure Claude sends the correct `Bearer` token.
- [ ] **Test:** Ask Claude a question that requires a "fetch" or "read" from Harmonise.

Does your Harmonise system require a specific OAuth 2.0 flow for individual user permissions, or will a single administrative API key for the whole Claude session suffice?