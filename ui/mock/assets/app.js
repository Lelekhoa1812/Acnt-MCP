(function () {
  const configNode = document.getElementById("mock-ui-config");
  const config = configNode ? JSON.parse(configNode.textContent || "{}") : {};
  const root = document.getElementById("root");

  if (!root) {
    return;
  }

  const SESSION_STORAGE_KEY = "hth-claude-desktop-sessions-v4";
  const DEBUG_STORAGE_KEY = "hth-claude-debug-mode-v4";
  const SIDEBAR_STORAGE_KEY = "hth-claude-sidebar-collapsed-v4";
  const PENDING_REQUEST_STORAGE_KEY = "hth-claude-pending-request-v1";
  const PENDING_REQUEST_STALE_MS = 20 * 60 * 1000;
  const STREAM_PERSIST_INTERVAL_MS = 180;
  const STREAM_LAYOUT_SYNC_INTERVAL_MS = 900;
  const GROUP_ORDER = ["Today", "Yesterday", "Last 7 Days", "Earlier"];
  const MAX_DEBUG_FRAMES = 120;
  const QUICK_PROMPTS = [
    "Compare fl-la-la-lam-1-ble vs fl-da-dan with stock and pricing.",
    "Show top low-stock variants from today’s catalogue search.",
    "Summarize weather and currency risk for supplier planning this week.",
    "Explain why a product request needs clarification and how to fix it.",
  ];

  const ICONS = {
    chevronLeft: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M15 6l-6 6 6 6" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    `,
    chevronRight: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    `,
    plus: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>
      </svg>
    `,
    pencil: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 20h9" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
        <path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4 12.5-12.5z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>
      </svg>
    `,
    trash: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M3 6h18M8 6V4h8v2m-9 0l1 13h8l1-13" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    `,
    bug: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M8 9V8a4 4 0 118 0v1m-8 0h8v7a4 4 0 11-8 0V9z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M3 13h4m10 0h4M5 7l2.5 2.5M19 7l-2.5 2.5M5 19l2.5-2.5M19 19l-2.5-2.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
    `,
    send: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M22 2L11 13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
        <path d="M22 2l-7 20-4-9-9-4 20-7z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
      </svg>
    `,
    sparkle: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6L12 3zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15zM5 14l.6 1.5L7 16l-1.4.5L5 18l-.6-1.5L3 16l1.4-.5L5 14z" fill="currentColor"/>
      </svg>
    `,
    user: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M20 21a8 8 0 00-16 0M12 11a4 4 0 100-8 4 4 0 000 8z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      </svg>
    `,
    keyboardReturn: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 7h12a4 4 0 014 4v0a4 4 0 01-4 4H8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
        <path d="M8 11l-4 4 4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    `,
    panelRight: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <rect x="3" y="4" width="18" height="16" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.7"/>
        <path d="M15 4v16" fill="none" stroke="currentColor" stroke-width="1.7"/>
      </svg>
    `,
    panelBottom: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <rect x="3" y="4" width="18" height="16" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.7"/>
        <path d="M3 14h18" fill="none" stroke="currentColor" stroke-width="1.7"/>
      </svg>
    `,
    clear: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 7h16M9 7V5h6v2m-8 0l1 12h8l1-12" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    `,
  };

  function icon(name, className) {
    const content = ICONS[name] || "";
    return `<span class="ui-icon ${className || ""}">${content}</span>`;
  }

  // Root Cause vs Logic: the previous runtime depended on external frontend
  // module imports and could render blank if those imports failed. This file is
  // now fully self-contained and ships without blocking JS module dependencies.
  const state = {
    sessions: [],
    activeSessionId: null,
    loadingSessions: true,
    renameSessionId: null,
    renameDraft: "",
    draftMessage: "",
    isSubmitting: false,
    debugEnabled: parseStoredBoolean(DEBUG_STORAGE_KEY, false),
    sidebarCollapsed: parseStoredBoolean(SIDEBAR_STORAGE_KEY, false),
    runtimeSummary: "Loading system metadata...",
    requestState: { label: "Ready", tone: "idle" },
    lastUpdated: "No request sent yet.",
    debugFrames: [],
    runtimeSpec: null,
    recoveryNotice: "",
  };

  const uiFlags = {
    renderQueued: false,
    focusRenameInput: false,
    focusComposer: false,
    feedShouldAutoScroll: true,
  };

  function createId(prefix) {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return `${prefix}-${window.crypto.randomUUID()}`;
    }
    return `${prefix}-${Math.random().toString(36).slice(2)}-${Date.now()}`;
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char];
    });
  }

  function formatInlineMarkdown(escaped) {
    return escaped
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/__(.+?)__/g, "<strong>$1</strong>")
      .replace(/\*(.+?)\*/g, "<em>$1</em>")
      .replace(/_(.+?)_/g, "<em>$1</em>");
  }

  function isTableDividerLine(line) {
    return /^\s*\|?(\s*-+\s*\|)+\s*-*\s*$/.test(line);
  }

  function parseTableRow(line) {
    let normalized = line.trim();
    if (normalized.startsWith("|")) {
      normalized = normalized.slice(1);
    }
    if (normalized.endsWith("|")) {
      normalized = normalized.slice(0, -1);
    }
    return normalized.split("|").map((cell) => cell.trim());
  }

  function renderTextSegment(text) {
    if (!text) {
      return "";
    }
    const escaped = escapeHtml(text);
    const normalizedHeadings = escaped.replace(/^#{1,6}\s+(.+)$/gm, "**$1**");
    return formatInlineMarkdown(normalizedHeadings).replace(/\n/g, "<br />");
  }

  function renderTableSegment(headers, rows) {
    const headerHtml = headers
      .map((header) => `<th>${formatInlineMarkdown(escapeHtml(header))}</th>`)
      .join("");
    const bodyHtml = rows
      .map((row) => {
        const cells = row.map((cell) => `<td>${formatInlineMarkdown(escapeHtml(cell))}</td>`).join("");
        return `<tr>${cells}</tr>`;
      })
      .join("");
    return `<table class="markdown-table"><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table>`;
  }

  function renderMarkdown(value) {
    if (value === undefined || value === null) {
      return "";
    }

    const lines = String(value).split("\n");
    const segments = [];
    let cursor = 0;

    while (cursor < lines.length) {
      const current = lines[cursor];
      if (current.trim().startsWith("|") && cursor + 1 < lines.length && isTableDividerLine(lines[cursor + 1])) {
        const tableLines = [current, lines[cursor + 1]];
        cursor += 2;
        while (cursor < lines.length && lines[cursor].trim().startsWith("|")) {
          tableLines.push(lines[cursor]);
          cursor += 1;
        }
        const headers = parseTableRow(tableLines[0]);
        const rows = tableLines.slice(2).map(parseTableRow).filter((row) => row.length);
        segments.push({ type: "table", headers, rows });
        continue;
      }

      const textBuffer = [];
      while (
        cursor < lines.length &&
        !(lines[cursor].trim().startsWith("|") && cursor + 1 < lines.length && isTableDividerLine(lines[cursor + 1]))
      ) {
        textBuffer.push(lines[cursor]);
        cursor += 1;
      }
      segments.push({ type: "text", content: textBuffer.join("\n") });
    }

    return segments
      .map((segment) => (segment.type === "table" ? renderTableSegment(segment.headers, segment.rows) : renderTextSegment(segment.content)))
      .join("\n");
  }

  function formatClock(timestamp) {
    return new Date(timestamp).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatRelativeTime(timestamp) {
    const value = new Date(timestamp).getTime();
    if (Number.isNaN(value)) {
      return "";
    }
    const diffMs = Date.now() - value;
    const diffMin = Math.round(diffMs / 60000);
    if (diffMin <= 0) {
      return "now";
    }
    if (diffMin < 60) {
      return `${diffMin}m`;
    }
    const diffHours = Math.round(diffMin / 60);
    if (diffHours < 24) {
      return `${diffHours}h`;
    }
    const diffDays = Math.round(diffHours / 24);
    if (diffDays < 7) {
      return `${diffDays}d`;
    }
    return new Date(timestamp).toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function estimateTokens(text) {
    return Math.max(1, Math.ceil(String(text || "").length / 4));
  }

  function getSessionGroup(dateValue) {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const date = new Date(dateValue);
    const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const diffDays = Math.floor((today - target) / 86400000);

    if (diffDays <= 0) {
      return "Today";
    }
    if (diffDays === 1) {
      return "Yesterday";
    }
    if (diffDays <= 7) {
      return "Last 7 Days";
    }
    return "Earlier";
  }

  function sortSessions(sessions) {
    return [...sessions].sort((left, right) => new Date(right.updatedAt) - new Date(left.updatedAt));
  }

  function groupSessions(sessions) {
    const groups = {
      Today: [],
      Yesterday: [],
      "Last 7 Days": [],
      Earlier: [],
    };
    sessions.forEach((session) => {
      groups[getSessionGroup(session.updatedAt)].push(session);
    });
    return groups;
  }

  function createMessage(role, content, options = {}) {
    return {
      id: options.id || createId("msg"),
      role,
      content,
      createdAt: options.createdAt || new Date().toISOString(),
      streaming: Boolean(options.streaming),
    };
  }

  function createSession(title, options = {}) {
    const createdAt = options.createdAt || new Date().toISOString();
    return {
      id: options.id || createId("session"),
      title,
      createdAt,
      updatedAt: options.updatedAt || createdAt,
      backendName: options.backendName || null,
      manualTitle: Boolean(options.manualTitle),
      messages: options.messages?.length
        ? options.messages
        : [
            createMessage(
              "assistant",
              "I’m ready. Ask about stock, compare variants, or inspect the current session with grounded tool calls.",
              { createdAt },
            ),
          ],
    };
  }

  function sanitizeMessage(raw) {
    if (!raw || typeof raw !== "object") {
      return null;
    }
    return createMessage(raw.role === "user" ? "user" : "assistant", String(raw.content || ""), {
      id: typeof raw.id === "string" ? raw.id : undefined,
      createdAt: typeof raw.createdAt === "string" ? raw.createdAt : undefined,
      streaming: false,
    });
  }

  function sanitizeSession(raw) {
    if (!raw || typeof raw !== "object") {
      return null;
    }

    const baseTitle = typeof raw.title === "string" && raw.title.trim() ? raw.title.trim() : "New chat";
    const messages = Array.isArray(raw.messages) ? raw.messages.map(sanitizeMessage).filter(Boolean) : [];
    const createdAt = typeof raw.createdAt === "string" ? raw.createdAt : new Date().toISOString();
    const updatedAt = typeof raw.updatedAt === "string" ? raw.updatedAt : createdAt;
    const manualTitle = raw.manualTitle === true;
    const backendName =
      typeof raw.backendName === "string"
        ? raw.backendName
        : typeof raw.session_name === "string"
        ? raw.session_name
        : null;
    const title = backendName && !manualTitle ? backendName : baseTitle;

    return createSession(title, {
      id: typeof raw.id === "string" ? raw.id : undefined,
      createdAt,
      updatedAt,
      messages,
      backendName,
      manualTitle,
    });
  }

  function buildSeedSessions() {
    const now = Date.now();
    return sortSessions([
      createSession("Laminate variants for venue", {
        createdAt: new Date(now - 45 * 60000).toISOString(),
        updatedAt: new Date(now - 12 * 60000).toISOString(),
        messages: [
          createMessage(
            "assistant",
            "Welcome to the desktop simulation. We can dig into live tool traces and normalize variant evidence as we go.",
            { createdAt: new Date(now - 45 * 60000).toISOString() },
          ),
          createMessage("user", "Can you compare fl-la-la-lam-1-ble with fl-da-dan?", {
            createdAt: new Date(now - 20 * 60000).toISOString(),
          }),
          createMessage(
            "assistant",
            "Yes. Share any pricing or stock thresholds and I’ll include them in the comparison logic.",
            { createdAt: new Date(now - 19 * 60000).toISOString() },
          ),
        ],
      }),
      createSession("Supplier weather impact notes", {
        createdAt: new Date(now - 27 * 3600000).toISOString(),
        updatedAt: new Date(now - 26 * 3600000).toISOString(),
      }),
      createSession("Currency sanity check", {
        createdAt: new Date(now - 3 * 86400000).toISOString(),
        updatedAt: new Date(now - 3 * 86400000 + 22 * 60000).toISOString(),
      }),
    ]);
  }

  function parseStoredSessions() {
    try {
      const raw = window.localStorage.getItem(SESSION_STORAGE_KEY);
      if (!raw) {
        return buildSeedSessions();
      }
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) {
        return buildSeedSessions();
      }
      const sessions = parsed.map(sanitizeSession).filter(Boolean);
      return sessions.length ? sortSessions(sessions) : buildSeedSessions();
    } catch (_error) {
      return buildSeedSessions();
    }
  }

  function parseStoredBoolean(key, defaultValue) {
    try {
      const value = window.localStorage.getItem(key);
      if (value === null) {
        return defaultValue;
      }
      return value === "true";
    } catch (_error) {
      return defaultValue;
    }
  }

  function parsePendingRequest() {
    try {
      const raw = window.localStorage.getItem(PENDING_REQUEST_STORAGE_KEY);
      if (!raw) {
        return null;
      }
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") {
        return null;
      }

      const requestPayload = parsed.requestPayload;
      if (!requestPayload || typeof requestPayload !== "object") {
        return null;
      }
      if (typeof requestPayload.message !== "string" || !requestPayload.message.trim()) {
        return null;
      }
      if (typeof requestPayload.sessionId !== "string" || !requestPayload.sessionId.trim()) {
        return null;
      }

      const sessionId = typeof parsed.sessionId === "string" ? parsed.sessionId : requestPayload.sessionId;
      const assistantMessageId = typeof parsed.assistantMessageId === "string" ? parsed.assistantMessageId : "";
      if (!sessionId || !assistantMessageId) {
        return null;
      }

      return {
        sessionId,
        assistantMessageId,
        userMessageId: typeof parsed.userMessageId === "string" ? parsed.userMessageId : null,
        requestPayload: {
          message: requestPayload.message,
          sessionId: requestPayload.sessionId,
          includeThoughts: requestPayload.includeThoughts !== false,
          renderMockUi: requestPayload.renderMockUi !== false,
        },
        startedAt: typeof parsed.startedAt === "string" ? parsed.startedAt : new Date().toISOString(),
        updatedAt: typeof parsed.updatedAt === "string" ? parsed.updatedAt : new Date().toISOString(),
        lastKnownAnswer: typeof parsed.lastKnownAnswer === "string" ? parsed.lastKnownAnswer : "",
      };
    } catch (_error) {
      return null;
    }
  }

  function savePendingRequest(record) {
    try {
      window.localStorage.setItem(PENDING_REQUEST_STORAGE_KEY, JSON.stringify(record));
    } catch (_error) {
      // no-op, localStorage quota/privacy mode
    }
  }

  function updatePendingRequest(update) {
    const current = parsePendingRequest();
    if (!current) {
      return;
    }
    savePendingRequest({
      ...current,
      ...update,
      updatedAt: new Date().toISOString(),
    });
  }

  function clearPendingRequest() {
    try {
      window.localStorage.removeItem(PENDING_REQUEST_STORAGE_KEY);
    } catch (_error) {
      // no-op
    }
  }

  function saveSessions() {
    window.localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(state.sessions));
  }

  function renderFatalScreen(error) {
    const detail = error instanceof Error ? error.message : String(error || "Unknown error");
    root.innerHTML = `
      <section class="fatal-screen">
        <div class="fatal-card">
          <p class="fatal-eyebrow">UI Recovery</p>
          <h1>We hit a rendering issue.</h1>
          <p>The interface did not disappear permanently. Refresh to continue, or reopen the session.</p>
          <pre>${escapeHtml(detail)}</pre>
        </div>
      </section>
    `;
  }

  function persistUiToggles() {
    window.localStorage.setItem(DEBUG_STORAGE_KEY, String(state.debugEnabled));
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(state.sidebarCollapsed));
  }

  function getActiveSession() {
    return state.sessions.find((session) => session.id === state.activeSessionId) || null;
  }

  function getSessionPreview(session) {
    if (!session.messages?.length) {
      return "No messages yet.";
    }
    const recent = session.messages[session.messages.length - 1];
    const compact = String(recent.content || "").replace(/\s+/g, " ").trim();
    if (!compact) {
      return recent.role === "assistant" ? "Assistant response pending…" : "Message drafted.";
    }
    return compact.length > 56 ? `${compact.slice(0, 56)}…` : compact;
  }

  function deriveSessionTitle(message) {
    const compact = String(message || "").replace(/\s+/g, " ").trim();
    if (!compact) {
      return "New chat";
    }
    return compact.length > 44 ? `${compact.slice(0, 44)}…` : compact;
  }

  function getBackendSessionTitle(payload) {
    const candidate = payload?.session_state?.session_name;
    if (typeof candidate !== "string") {
      return "";
    }
    return candidate.trim();
  }

  function applyBackendSessionTitle(sessionId, payload) {
    const backendSessionTitle = getBackendSessionTitle(payload);
    if (!backendSessionTitle) {
      return;
    }

    const session = state.sessions.find((entry) => entry.id === sessionId);
    if (!session) {
      return;
    }

    session.backendName = backendSessionTitle;
    if (!session.manualTitle) {
      session.title = backendSessionTitle;
    }
    session.updatedAt = new Date().toISOString();
    state.sessions = sortSessions(state.sessions);
    saveSessions();
    queueRender();
  }

  function mutateStreamingMessage(sessionId, messageId, content, streaming) {
    const session = state.sessions.find((entry) => entry.id === sessionId);
    if (!session) {
      return false;
    }
    const message = session.messages.find((entry) => entry.id === messageId);
    if (!message) {
      return false;
    }
    message.content = content;
    message.streaming = streaming;
    session.updatedAt = new Date().toISOString();
    return true;
  }

  function patchStreamingMessage(messageId, content, streaming) {
    const contentNode = root.querySelector(`[data-role="bubble-content"][data-message-id="${messageId}"]`);
    if (contentNode) {
      contentNode.innerHTML = renderMarkdown(content || "");
    }

    const row = root.querySelector(`.message-row[data-message-id="${messageId}"]`);
    if (!row) {
      return;
    }

    const bubble = row.querySelector(".bubble");
    if (!bubble) {
      return;
    }

    let cursor = row.querySelector('[data-role="stream-cursor"]');
    if (streaming) {
      if (!cursor) {
        cursor = document.createElement("span");
        cursor.className = "stream-cursor";
        cursor.dataset.role = "stream-cursor";
        bubble.appendChild(cursor);
      }
    } else if (cursor) {
      cursor.remove();
    }
  }

  function ensurePendingConversationState(pending) {
    const session = state.sessions.find((entry) => entry.id === pending.sessionId);
    if (!session) {
      return { ok: false, completed: false };
    }

    const assistantMessage = session.messages.find((entry) => entry.id === pending.assistantMessageId);
    if (assistantMessage && !assistantMessage.streaming && String(assistantMessage.content || "").trim()) {
      return { ok: false, completed: true };
    }

    if (!session.messages.some((entry) => entry.id === pending.userMessageId) && pending.requestPayload.message) {
      session.messages.push(
        createMessage("user", pending.requestPayload.message, {
          id: pending.userMessageId || createId("msg"),
          createdAt: pending.startedAt,
        }),
      );
    }

    if (!assistantMessage) {
      session.messages.push(
        createMessage("assistant", pending.lastKnownAnswer || "", {
          id: pending.assistantMessageId,
          createdAt: pending.startedAt,
          streaming: true,
        }),
      );
    } else {
      if (pending.lastKnownAnswer && pending.lastKnownAnswer.length > String(assistantMessage.content || "").length) {
        assistantMessage.content = pending.lastKnownAnswer;
      }
      assistantMessage.streaming = true;
    }

    session.updatedAt = new Date().toISOString();
    state.activeSessionId = pending.sessionId;
    state.sessions = sortSessions(state.sessions);
    saveSessions();
    return { ok: true, completed: false };
  }

  function updateSession(sessionId, updater) {
    state.sessions = sortSessions(
      state.sessions.map((session) => {
        if (session.id !== sessionId) {
          return session;
        }
        return updater(session);
      }),
    );
    saveSessions();
    queueRender();
  }

  function createAndActivateSession(seedTitle) {
    const session = createSession(seedTitle || "New chat");
    state.sessions = sortSessions([session, ...state.sessions]);
    state.activeSessionId = session.id;
    saveSessions();
    return session.id;
  }

  function pushDebugFrame(frame) {
    state.debugFrames = [frame, ...state.debugFrames].slice(0, MAX_DEBUG_FRAMES);
    queueRender();
  }

  function extractTokenUsage(payload, requestPayload, answer) {
    const usage = payload?.token_usage || payload?.usage || {};
    const hasPayloadTokens =
      usage.prompt_tokens !== undefined ||
      usage.input_tokens !== undefined ||
      usage.completion_tokens !== undefined ||
      usage.output_tokens !== undefined ||
      usage.total_tokens !== undefined;

    const prompt = hasPayloadTokens
      ? usage.prompt_tokens ?? usage.input_tokens ?? estimateTokens(JSON.stringify(requestPayload))
      : estimateTokens(JSON.stringify(requestPayload));
    const completion = hasPayloadTokens
      ? usage.completion_tokens ?? usage.output_tokens ?? estimateTokens(answer)
      : estimateTokens(answer);

    return {
      prompt: Number(prompt),
      completion: Number(completion),
      total: Number(usage.total_tokens ?? prompt + completion),
      source: hasPayloadTokens ? "payload" : "estimated",
    };
  }

  function buildModelMetadata(payload, sessionId) {
    return {
      status: payload?.status || "unknown",
      sessionId,
      toolCalls: Array.isArray(payload?.tool_trace) ? payload.tool_trace.length : 0,
      thoughtBlocks: Array.isArray(payload?.thoughts) ? payload.thoughts.length : 0,
      limitations: payload?.limitations || [],
      service: state.runtimeSpec?.server_name || config.serviceName || "HTH MCP",
      serviceVersion: state.runtimeSpec?.server_version || config.serviceVersion || "unknown",
      model: payload?.model || "not_exposed_by_backend",
    };
  }

  function queueRender() {
    if (uiFlags.renderQueued) {
      return;
    }
    uiFlags.renderQueued = true;
    window.requestAnimationFrame(() => {
      uiFlags.renderQueued = false;
      try {
        render();
      } catch (error) {
        console.error("HTH UI render failure", error);
        renderFatalScreen(error);
      }
    });
  }

  function renderStatusPill() {
    const tone = escapeHtml(state.requestState.tone || "idle");
    const label = escapeHtml(state.requestState.label || "Ready");
    return `<span class="status-pill ${tone}">${label}</span>`;
  }

  function renderSessionSkeleton() {
    return `
      <div class="skeleton-list">
        ${Array.from({ length: 10 })
          .map(() => `<div class="skeleton-row"></div>`)
          .join("")}
      </div>
    `;
  }

  function renderSessionsList() {
    if (state.loadingSessions) {
      return renderSessionSkeleton();
    }

    const groups = groupSessions(state.sessions);
    return GROUP_ORDER.map((groupName) => {
      const entries = groups[groupName];
      if (!entries.length) {
        return "";
      }

      return `
        <section class="session-group">
          ${state.sidebarCollapsed ? "" : `<p class="group-title">${groupName}</p>`}
          <div class="session-stack">
            ${entries
              .map((session) => {
                const isActive = session.id === state.activeSessionId;
                const isRenaming = state.renameSessionId === session.id && !state.sidebarCollapsed;
                const preview = getSessionPreview(session);
                return `
                  <article class="session-item ${isActive ? "active" : ""}">
                    ${
                      isRenaming
                        ? `
                          <input
                            data-role="rename-input"
                            data-session-id="${escapeHtml(session.id)}"
                            value="${escapeHtml(state.renameDraft)}"
                            class="session-input"
                            aria-label="Rename conversation"
                          />
                        `
                        : `
                          <button
                            type="button"
                            class="session-select"
                            data-action="switch-session"
                            data-session-id="${escapeHtml(session.id)}"
                            title="${escapeHtml(session.title)}"
                            aria-label="Open ${escapeHtml(session.title)}"
                          >
                            <span class="session-title">${state.sidebarCollapsed ? escapeHtml(session.title.slice(0, 1).toUpperCase()) : escapeHtml(session.title)}</span>
                            ${state.sidebarCollapsed ? "" : `<span class="session-time">${escapeHtml(formatRelativeTime(session.updatedAt))}</span>`}
                          </button>
                          ${
                            state.sidebarCollapsed
                              ? ""
                              : `
                                <div class="session-foot">
                                  <p class="session-preview">${escapeHtml(preview)}</p>
                                  <div class="session-actions">
                                    <button
                                      type="button"
                                      class="icon-action"
                                      data-action="start-rename"
                                      data-session-id="${escapeHtml(session.id)}"
                                      title="Rename session"
                                      aria-label="Rename session"
                                    >
                                      ${icon("pencil")}
                                    </button>
                                    <button
                                      type="button"
                                      class="icon-action danger"
                                      data-action="delete-session"
                                      data-session-id="${escapeHtml(session.id)}"
                                      title="Delete session"
                                      aria-label="Delete session"
                                    >
                                      ${icon("trash")}
                                    </button>
                                  </div>
                                </div>
                              `
                          }
                        `
                    }
                  </article>
                `;
              })
              .join("")}
          </div>
        </section>
      `;
    }).join("");
  }

  function renderMessages() {
    const session = getActiveSession();
    const messages = session?.messages || [];

    return messages
      .map((message, index) => {
        const roleClass = message.role === "user" ? "user" : "assistant";
        const animateClass = index >= messages.length - 2 ? "message-enter" : "";
        const avatar = message.role === "user" ? icon("user") : icon("sparkle");
        return `
          <article
            class="message-row ${roleClass} ${animateClass}"
            data-message-id="${escapeHtml(message.id)}"
            style="--stagger:${Math.max(index - (messages.length - 2), 0) * 34}ms"
          >
            <div class="message-unit ${roleClass}">
              <div class="avatar ${roleClass}">${avatar}</div>
              <div class="bubble ${roleClass}">
                <div class="bubble-content" data-role="bubble-content" data-message-id="${escapeHtml(message.id)}">${renderMarkdown(message.content)}</div>
                <div class="bubble-meta ${roleClass}">
                  <span class="bubble-role">${message.role === "user" ? "You" : "Claude"}</span>
                  <span class="bubble-time">${escapeHtml(formatClock(message.createdAt || new Date().toISOString()))}</span>
                </div>
                ${message.streaming ? `<span class="stream-cursor" data-role="stream-cursor"></span>` : ""}
              </div>
            </div>
          </article>
        `;
      })
      .join("");
  }

  function renderPromptChips() {
    const session = getActiveSession();
    if (!session || session.messages.some((entry) => entry.role === "user")) {
      return "";
    }

    return `
      <section class="prompt-chips">
        <p class="prompt-title">Start with one of these production demos</p>
        <div class="prompt-grid">
          ${QUICK_PROMPTS.map(
            (prompt) => `
              <button type="button" class="prompt-chip" data-action="apply-suggestion" data-prompt="${escapeHtml(prompt)}">
                ${escapeHtml(prompt)}
              </button>
            `,
          ).join("")}
        </div>
      </section>
    `;
  }

  function captureFeedScrollState() {
    const feed = root.querySelector('[data-role="message-feed"]');
    if (!feed) {
      return null;
    }
    return {
      scrollTop: feed.scrollTop,
      distanceFromBottom: feed.scrollHeight - (feed.scrollTop + feed.clientHeight),
    };
  }

  function renderDebugStream() {
    if (!state.debugEnabled) {
      return "";
    }

    return `
      <section class="debug-drawer">
        <div class="debug-head">
          <div class="debug-title-wrap">
            ${icon("bug", "debug-title-icon")}
            <div>
              <p class="debug-title">System Debug Stream</p>
              <p class="debug-caption">${escapeHtml(config.queryEndpoint || "/api/v1/query")} · real-time request telemetry</p>
            </div>
          </div>
          <button type="button" class="ghost-btn" data-action="clear-debug">
            ${icon("clear")}
            <span>Clear</span>
          </button>
        </div>
        <div class="debug-feed">
          ${
            state.debugFrames.length
              ? state.debugFrames
                  .map((frame, index) => {
                    const tokens = frame.tokenUsage?.total ?? 0;
                    const latency = frame.latencyMs ?? "pending";
                    const toolCalls = frame.modelMetadata?.toolCalls ?? 0;
                    return `
                      <article class="debug-card">
                        <div class="debug-meta">
                          <span>${escapeHtml(frame.event || "Frame")}</span>
                          <span>${escapeHtml(formatClock(frame.timestamp || new Date().toISOString()))}</span>
                        </div>
                        <div class="debug-metrics">
                          <span class="metric-chip">${tokens} tokens</span>
                          <span class="metric-chip">${latency} ms</span>
                          <span class="metric-chip">${toolCalls} tool calls</span>
                        </div>
                        <details class="debug-details" ${index === 0 ? "open" : ""}>
                          <summary>Raw JSON</summary>
                          <pre class="debug-json">${escapeHtml(JSON.stringify(frame, null, 2))}</pre>
                        </details>
                      </article>
                    `;
                  })
                  .join("")
              : `<div class="debug-empty">Run a message to stream Raw API Request, Token Usage, Latency (ms), and Model Metadata.</div>`
          }
        </div>
      </section>
    `;
  }

  function render() {
    const preservedFeedState = captureFeedScrollState();
    const activeSession = getActiveSession();
    const assignedSessionName = activeSession?.backendName;

    root.innerHTML = `
      <div class="claude-shell ${state.sidebarCollapsed ? "sidebar-collapsed" : ""}">
        <aside class="claude-sidebar">
          <div class="sidebar-top">
            <div class="sidebar-brand ${state.sidebarCollapsed ? "collapsed" : ""}">
              ${
                state.sidebarCollapsed
                  ? ""
                  : `
                    <p class="eyebrow">Claude Desktop</p>
                    <p class="brand-text">${escapeHtml(config.serviceName || "HTH MCP")}</p>
                  `
              }
            </div>
            <button class="icon-button" type="button" data-action="toggle-sidebar" aria-label="Toggle sidebar">
              ${state.sidebarCollapsed ? icon("chevronRight") : icon("chevronLeft")}
            </button>
          </div>

          <button class="new-chat-btn" type="button" data-action="new-chat">
            ${icon("plus")}
            ${state.sidebarCollapsed ? "" : "<span>New Chat</span>"}
          </button>

          <div class="session-list">
            ${renderSessionsList()}
          </div>

          <div class="sidebar-footer">
            ${
              state.sidebarCollapsed
                ? ""
                : `
                  <div class="runtime-card">
                    <p class="runtime-title">Runtime</p>
                    <p class="runtime-text">${escapeHtml(state.runtimeSummary)}</p>
                  </div>
                `
            }
            <label class="debug-toggle ${state.sidebarCollapsed ? "compact" : ""}">
              <input type="checkbox" data-role="debug-toggle" ${state.debugEnabled ? "checked" : ""} />
              <span class="toggle-label">
                ${state.sidebarCollapsed ? icon("bug") : `${icon("bug")}Enable Debug Mode`}
              </span>
            </label>
          </div>
        </aside>

        <main class="claude-main ${state.debugEnabled ? "debug-open" : ""}">
          <section class="stage">
            <header class="stage-header">
              <div class="stage-title">
                <p class="eyebrow">Unified Conversation</p>
                <h1>${escapeHtml(activeSession?.title || "New chat")}</h1>
                ${
                  assignedSessionName
                    ? `<p class="stage-session-name">Assigned: ${escapeHtml(assignedSessionName)}</p>`
                    : ""
                }
                ${state.recoveryNotice ? `<p class="stage-recovery">${escapeHtml(state.recoveryNotice)}</p>` : ""}
              </div>
              <div class="stage-status">
                ${renderStatusPill()}
                <p class="updated-at">${escapeHtml(state.lastUpdated)}</p>
              </div>
            </header>

            <div class="message-feed" data-role="message-feed">
              <div class="feed-inner">
                ${renderMessages()}
                ${renderPromptChips()}
              </div>
            </div>

            <footer class="composer-wrap">
              <form class="composer-form" data-role="composer-form">
                <textarea
                  class="composer-input"
                  data-role="composer-input"
                  rows="1"
                  placeholder="Message Claude"
                  aria-label="Message composer"
                >${escapeHtml(state.draftMessage)}</textarea>
                <div class="composer-bottom">
                  <div class="composer-hints">
                    <span class="hint-chip">${icon("keyboardReturn")}Shift+Enter for newline</span>
                    <span class="hint-chip">${icon("panelRight")}⌘/Ctrl+B toggle sidebar</span>
                    <span class="hint-chip">${icon("panelBottom")}⌘/Ctrl+Shift+D debug</span>
                  </div>
                  <button type="submit" class="send-btn" data-action="send-message" ${state.isSubmitting ? "disabled" : ""}>
                    ${icon("send")}
                    <span>${state.isSubmitting ? "Sending…" : "Send"}</span>
                  </button>
                </div>
              </form>
            </footer>
          </section>
          ${renderDebugStream()}
        </main>
      </div>
    `;

    postRenderEffects(preservedFeedState);
  }

  function postRenderEffects(preservedFeedState) {
    const feed = root.querySelector('[data-role="message-feed"]');
    if (feed) {
      const clampScroll = 32;
      const updateAutoScrollFlag = () => {
        const distanceFromBottom = feed.scrollHeight - (feed.scrollTop + feed.clientHeight);
        uiFlags.feedShouldAutoScroll = distanceFromBottom <= clampScroll;
      };

      feed.addEventListener("scroll", updateAutoScrollFlag);
      const shouldAlwaysStickToBottom =
        uiFlags.feedShouldAutoScroll ||
        (preservedFeedState?.distanceFromBottom !== undefined && preservedFeedState.distanceFromBottom <= clampScroll);

      if (shouldAlwaysStickToBottom) {
        feed.scrollTop = feed.scrollHeight;
      } else if (preservedFeedState) {
        // Root Cause vs Logic: re-rendering replaces the DOM nodes, so we must restore the
        // remembered scroll offset when auto-scroll is off instead of jumping back to the top.
        const maxScrollTop = Math.max(0, feed.scrollHeight - feed.clientHeight);
        feed.scrollTop = Math.min(preservedFeedState.scrollTop, maxScrollTop);
      }
      updateAutoScrollFlag();
    }

    const composerInput = root.querySelector('[data-role="composer-input"]');
    if (composerInput) {
      composerInput.style.height = "0px";
      composerInput.style.height = `${Math.min(220, composerInput.scrollHeight)}px`;
      if (uiFlags.focusComposer) {
        composerInput.focus();
        uiFlags.focusComposer = false;
      }
    }

    if (uiFlags.focusRenameInput) {
      const renameInput = root.querySelector('[data-role="rename-input"]');
      if (renameInput) {
        renameInput.focus();
        renameInput.select();
      }
      uiFlags.focusRenameInput = false;
    }
  }

  function beginRename(sessionId) {
    const session = state.sessions.find((entry) => entry.id === sessionId);
    if (!session) {
      return;
    }
    state.renameSessionId = sessionId;
    state.renameDraft = session.title;
    uiFlags.focusRenameInput = true;
    queueRender();
  }

  function commitRename() {
    if (!state.renameSessionId) {
      return;
    }
    const targetSessionId = state.renameSessionId;
    const nextName = state.renameDraft.trim() || "Untitled chat";
    state.renameSessionId = null;
    state.renameDraft = "";
    updateSession(targetSessionId, (session) => ({
      ...session,
      title: nextName,
      manualTitle: true,
      updatedAt: new Date().toISOString(),
    }));
  }

  function cancelRename() {
    state.renameSessionId = null;
    state.renameDraft = "";
    queueRender();
  }

  function removeSession(sessionId) {
    const pending = parsePendingRequest();
    if (pending && pending.sessionId === sessionId) {
      clearPendingRequest();
    }

    state.sessions = state.sessions.filter((session) => session.id !== sessionId);

    if (!state.sessions.length) {
      const fallback = createSession("New chat");
      state.sessions = [fallback];
      state.activeSessionId = fallback.id;
    } else if (state.activeSessionId === sessionId) {
      state.activeSessionId = state.sessions[0].id;
    }

    state.sessions = sortSessions(state.sessions);
    state.renameSessionId = null;
    state.renameDraft = "";
    saveSessions();
    queueRender();
  }

  function resetForNewChat() {
    clearPendingRequest();
    state.renameSessionId = null;
    state.renameDraft = "";
    state.draftMessage = "";
    state.recoveryNotice = "";
    state.requestState = { tone: "idle", label: "Ready" };
    state.lastUpdated = "No request sent yet.";
    uiFlags.focusComposer = true;
    uiFlags.feedShouldAutoScroll = true;
    queueRender();
  }

  function applyPromptSuggestion(prompt) {
    state.draftMessage = prompt;
    uiFlags.focusComposer = true;
    queueRender();
  }

  function onActionClick(action, target) {
    const sessionId = target.dataset.sessionId;
    if (action === "toggle-sidebar") {
      state.sidebarCollapsed = !state.sidebarCollapsed;
      persistUiToggles();
      queueRender();
      return;
    }

    if (action === "new-chat") {
      createAndActivateSession("New chat");
      resetForNewChat();
      return;
    }

    if (action === "switch-session" && sessionId) {
      state.activeSessionId = sessionId;
      state.renameSessionId = null;
      state.renameDraft = "";
      queueRender();
      return;
    }

    if (action === "start-rename" && sessionId) {
      beginRename(sessionId);
      return;
    }

    if (action === "delete-session" && sessionId) {
      removeSession(sessionId);
      return;
    }

    if (action === "apply-suggestion") {
      const prompt = target.dataset.prompt || "";
      if (prompt) {
        applyPromptSuggestion(prompt);
      }
      return;
    }

    if (action === "clear-debug") {
      state.debugFrames = [];
      queueRender();
    }
  }

  function handleRootClick(event) {
    const actionTarget = event.target.closest("[data-action]");
    if (!actionTarget) {
      return;
    }
    onActionClick(actionTarget.dataset.action, actionTarget);
  }

  function handleRootInput(event) {
    const role = event.target.dataset.role;
    if (role === "composer-input") {
      state.draftMessage = event.target.value;
      event.target.style.height = "0px";
      event.target.style.height = `${Math.min(220, event.target.scrollHeight)}px`;
      return;
    }

    if (role === "rename-input") {
      state.renameDraft = event.target.value;
    }
  }

  function handleRootChange(event) {
    const role = event.target.dataset.role;
    if (role === "debug-toggle") {
      state.debugEnabled = event.target.checked;
      persistUiToggles();
      queueRender();
    }
  }

  function handleRootKeyDown(event) {
    const role = event.target.dataset.role;
    if (role === "composer-input" && event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSubmit();
      return;
    }

    if (role === "rename-input") {
      if (event.key === "Enter") {
        event.preventDefault();
        commitRename();
      }
      if (event.key === "Escape") {
        event.preventDefault();
        cancelRename();
      }
    }
  }

  function handleRootFocusOut(event) {
    if (event.target.dataset.role === "rename-input") {
      commitRename();
    }
  }

  function handleRootSubmit(event) {
    if (event.target.dataset.role !== "composer-form") {
      return;
    }
    event.preventDefault();
    handleSubmit();
  }

  function handleGlobalShortcuts(event) {
    const meta = event.metaKey || event.ctrlKey;
    if (!meta) {
      return;
    }
    const tag = event.target?.tagName?.toLowerCase();
    const inEditable = tag === "textarea" || tag === "input" || event.target?.isContentEditable;

    if (event.key.toLowerCase() === "b" && !event.shiftKey) {
      event.preventDefault();
      state.sidebarCollapsed = !state.sidebarCollapsed;
      persistUiToggles();
      queueRender();
      return;
    }

    if (event.key.toLowerCase() === "d" && event.shiftKey) {
      event.preventDefault();
      state.debugEnabled = !state.debugEnabled;
      persistUiToggles();
      queueRender();
      return;
    }

    if (event.key.toLowerCase() === "n" && !event.shiftKey) {
      event.preventDefault();
      createAndActivateSession("New chat");
      resetForNewChat();
      return;
    }

    if (event.key.toLowerCase() === "k" && !event.shiftKey) {
      event.preventDefault();
      uiFlags.focusComposer = true;
      if (!inEditable) {
        queueRender();
      }
    }
  }

  async function streamAssistantText(sessionId, messageId, text, onProgress) {
    if (!text) {
      if (mutateStreamingMessage(sessionId, messageId, "", false)) {
        saveSessions();
        patchStreamingMessage(messageId, "", false);
        state.sessions = sortSessions(state.sessions);
        queueRender();
      }
      if (onProgress) {
        onProgress("", true);
      }
      return;
    }

    let cursor = 0;
    let lastPersist = 0;
    let lastLayoutSync = 0;

    while (cursor < text.length) {
      const step = Math.min(text.length - cursor, 2 + Math.floor(Math.random() * 9));
      cursor += step;
      const chunk = text.slice(0, cursor);
      const now = performance.now();

      if (!mutateStreamingMessage(sessionId, messageId, chunk, true)) {
        break;
      }

      patchStreamingMessage(messageId, chunk, true);

      if (now - lastPersist >= STREAM_PERSIST_INTERVAL_MS) {
        saveSessions();
        if (onProgress) {
          onProgress(chunk, false);
        }
        lastPersist = now;
      }

      if (now - lastLayoutSync >= STREAM_LAYOUT_SYNC_INTERVAL_MS) {
        state.sessions = sortSessions(state.sessions);
        queueRender();
        lastLayoutSync = now;
      }

      await new Promise((resolve) => window.setTimeout(resolve, 22));
    }

    if (mutateStreamingMessage(sessionId, messageId, text, false)) {
      saveSessions();
      patchStreamingMessage(messageId, text, false);
      state.sessions = sortSessions(state.sessions);
      queueRender();
    }
    if (onProgress) {
      onProgress(text, true);
    }
  }

  async function submitQuery(requestPayload) {
    const response = await fetch(config.queryEndpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(requestPayload),
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || "Query request failed.");
    }
    return payload;
  }

  // Motivation vs Logic: production chat UX should feel instant and stable even
  // with network latency, so we apply optimistic bubbles, stream updates, and
  // reconcile backend naming/metadata before finalizing the visible state.
  async function handleSubmit() {
    if (state.isSubmitting) {
      return;
    }

    const message = state.draftMessage.trim();
    if (!message) {
      const input = root.querySelector('[data-role="composer-input"]');
      if (input) {
        input.focus();
      }
      return;
    }

    const currentSessionId = state.activeSessionId || createAndActivateSession("New chat");
    const nowIso = new Date().toISOString();
    const userMessage = createMessage("user", message, { createdAt: nowIso });
    const assistantMessage = createMessage("assistant", "", { createdAt: nowIso, streaming: true });

    updateSession(currentSessionId, (session) => ({
      ...session,
      title:
        session.title === "New chat" || session.title === "Untitled chat"
          ? deriveSessionTitle(message)
          : session.title,
      updatedAt: nowIso,
      messages: [...session.messages, userMessage, assistantMessage],
    }));

    const requestPayload = {
      message,
      sessionId: currentSessionId,
      includeThoughts: true,
      renderMockUi: true,
    };

    savePendingRequest({
      sessionId: currentSessionId,
      assistantMessageId: assistantMessage.id,
      userMessageId: userMessage.id,
      requestPayload,
      startedAt: nowIso,
      updatedAt: nowIso,
      lastKnownAnswer: "",
    });

    state.draftMessage = "";
    state.isSubmitting = true;
    state.recoveryNotice = "";
    state.requestState = { tone: "running", label: "Running" };
    state.lastUpdated = `Sent at ${formatClock(nowIso)}`;
    uiFlags.feedShouldAutoScroll = true;
    queueRender();

    const startedAt = performance.now();
    pushDebugFrame({
      timestamp: nowIso,
      event: "Raw API Request",
      rawApiRequest: requestPayload,
      tokenUsage: {
        prompt: estimateTokens(message),
        completion: 0,
        total: estimateTokens(message),
        source: "estimated",
      },
      latencyMs: null,
      modelMetadata: {
        service: state.runtimeSpec?.server_name || config.serviceName || "HTH MCP",
        serviceVersion: state.runtimeSpec?.server_version || config.serviceVersion || "unknown",
      },
    });

    try {
      const payload = await submitQuery(requestPayload);
      const answer = payload.answer?.trim() || "No answer returned.";
      const latencyMs = Math.round(performance.now() - startedAt);
      const tokenUsage = extractTokenUsage(payload, requestPayload, answer);
      const modelMetadata = buildModelMetadata(payload, currentSessionId);
      applyBackendSessionTitle(currentSessionId, payload);

      pushDebugFrame({
        timestamp: new Date().toISOString(),
        event: "Model Response",
        rawApiRequest: requestPayload,
        tokenUsage,
        latencyMs,
        modelMetadata,
      });

      await streamAssistantText(currentSessionId, assistantMessage.id, answer, (chunk, done) => {
        updatePendingRequest({
          lastKnownAnswer: chunk,
          completed: done === true,
        });
      });
      clearPendingRequest();
      state.requestState = { tone: payload.status || "answered", label: payload.status || "Answered" };
      state.lastUpdated = `Last response at ${formatClock(new Date())}`;
    } catch (error) {
      const latencyMs = Math.round(performance.now() - startedAt);
      const fallback = "The request failed. Check backend availability and try again.";

      updateSession(currentSessionId, (session) => ({
        ...session,
        updatedAt: new Date().toISOString(),
        messages: session.messages.map((entry) =>
          entry.id === assistantMessage.id ? { ...entry, content: fallback, streaming: false } : entry,
        ),
      }));

      pushDebugFrame({
        timestamp: new Date().toISOString(),
        event: "Error",
        rawApiRequest: requestPayload,
        tokenUsage: {
          prompt: estimateTokens(message),
          completion: 0,
          total: estimateTokens(message),
          source: "estimated",
        },
        latencyMs,
        modelMetadata: {
          status: "error",
          detail: error instanceof Error ? error.message : "Unknown error",
          service: state.runtimeSpec?.server_name || config.serviceName || "HTH MCP",
          serviceVersion: state.runtimeSpec?.server_version || config.serviceVersion || "unknown",
        },
      });

      state.requestState = { tone: "error", label: "Error" };
      state.lastUpdated = `Request failed at ${formatClock(new Date())}`;
      clearPendingRequest();
    } finally {
      state.isSubmitting = false;
      queueRender();
    }
  }

  async function recoverPendingRequest(pending) {
    const startedAt = performance.now();
    try {
      const payload = await submitQuery(pending.requestPayload);
      const answer = payload.answer?.trim() || "No answer returned.";
      const latencyMs = Math.round(performance.now() - startedAt);
      const tokenUsage = extractTokenUsage(payload, pending.requestPayload, answer);
      const modelMetadata = buildModelMetadata(payload, pending.sessionId);

      applyBackendSessionTitle(pending.sessionId, payload);

      pushDebugFrame({
        timestamp: new Date().toISOString(),
        event: "Recovered Response",
        rawApiRequest: pending.requestPayload,
        tokenUsage,
        latencyMs,
        modelMetadata,
      });

      await streamAssistantText(pending.sessionId, pending.assistantMessageId, answer, (chunk, done) => {
        updatePendingRequest({
          lastKnownAnswer: chunk,
          completed: done === true,
        });
      });

      state.requestState = { tone: payload.status || "answered", label: payload.status || "Answered" };
      state.lastUpdated = `Recovered at ${formatClock(new Date())}`;
    } catch (error) {
      const fallback = "The previous response was interrupted after refresh. Retry to continue.";
      mutateStreamingMessage(pending.sessionId, pending.assistantMessageId, fallback, false);
      patchStreamingMessage(pending.assistantMessageId, fallback, false);
      saveSessions();
      state.sessions = sortSessions(state.sessions);
      pushDebugFrame({
        timestamp: new Date().toISOString(),
        event: "Recovery Error",
        rawApiRequest: pending.requestPayload,
        tokenUsage: {
          prompt: estimateTokens(pending.requestPayload.message),
          completion: 0,
          total: estimateTokens(pending.requestPayload.message),
          source: "estimated",
        },
        latencyMs: Math.round(performance.now() - startedAt),
        modelMetadata: {
          status: "error",
          detail: error instanceof Error ? error.message : "Unknown error",
          service: state.runtimeSpec?.server_name || config.serviceName || "HTH MCP",
          serviceVersion: state.runtimeSpec?.server_version || config.serviceVersion || "unknown",
        },
      });
      state.requestState = { tone: "error", label: "Recovery failed" };
      state.lastUpdated = `Recovery failed at ${formatClock(new Date())}`;
    } finally {
      clearPendingRequest();
      state.isSubmitting = false;
      queueRender();
    }
  }

  function resumePendingRequestIfNeeded() {
    const pending = parsePendingRequest();
    if (!pending) {
      return;
    }

    const startedMs = new Date(pending.startedAt).getTime();
    if (Number.isNaN(startedMs) || Date.now() - startedMs > PENDING_REQUEST_STALE_MS) {
      clearPendingRequest();
      return;
    }

    const ensured = ensurePendingConversationState(pending);
    if (ensured.completed) {
      clearPendingRequest();
      return;
    }
    if (!ensured.ok) {
      clearPendingRequest();
      return;
    }

    state.isSubmitting = true;
    state.requestState = { tone: "running", label: "Recovering" };
    state.lastUpdated = "Recovering interrupted response…";
    state.recoveryNotice = "Recovered an in-progress response after refresh.";
    uiFlags.feedShouldAutoScroll = true;
    queueRender();
    void recoverPendingRequest(pending);
  }

  async function hydrateRuntimeSummary() {
    try {
      const response = await fetch(config.systemSpecEndpoint);
      if (!response.ok) {
        throw new Error("System spec request failed.");
      }
      const payload = await response.json();
      state.runtimeSpec = payload;
      state.runtimeSummary =
        Array.isArray(payload.scope) && payload.scope.length
          ? payload.scope.slice(0, 2).join(" · ")
          : "Simulation ready with live MCP query routing.";
    } catch (_error) {
      state.runtimeSummary = "Simulation ready with live MCP query routing.";
    }
    queueRender();
  }

  function boot() {
    try {
      render();
    } catch (error) {
      console.error("HTH UI initial render failure", error);
      renderFatalScreen(error);
      return;
    }

    window.setTimeout(() => {
      state.sessions = parseStoredSessions();
      state.activeSessionId = state.sessions[0]?.id || null;
      state.loadingSessions = false;
      saveSessions();
      queueRender();
      resumePendingRequestIfNeeded();
    }, 320);

    hydrateRuntimeSummary();
  }

  root.addEventListener("click", handleRootClick);
  root.addEventListener("input", handleRootInput);
  root.addEventListener("change", handleRootChange);
  root.addEventListener("keydown", handleRootKeyDown);
  root.addEventListener("focusout", handleRootFocusOut);
  root.addEventListener("submit", handleRootSubmit);
  window.addEventListener("keydown", handleGlobalShortcuts);
  window.addEventListener("error", (event) => {
    console.error("HTH UI runtime error", event.error || event.message);
  });
  window.addEventListener("unhandledrejection", (event) => {
    console.error("HTH UI unhandled rejection", event.reason);
  });

  boot();
})();
