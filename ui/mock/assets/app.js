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
  const MAX_SESSION_NAME_WORDS_FALLBACK = 4;
  const NEW_CHAT_TITLE = "New chat";
  const UNTITLED_CHAT_TITLE = "Untitled chat";
  const LEGACY_READY_MESSAGE =
    "I’m ready. Ask about stock, compare variants, or inspect the current session with grounded tool calls.";
  const GROUP_ORDER = ["Today", "Yesterday", "Last 7 Days", "Earlier"];
  const MAX_DEBUG_FRAMES = 120;
  const QUICK_PROMPTS = [
    "Check stock for Laminate Bleached Elm.",
    "Compare Laminate Bleached Elm and Dark Ash stock levels.",
    "Show products that are low in stock today.",
    "List the most available products right now.",
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
    stopCircle: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="8.4" fill="none" stroke="currentColor" stroke-width="1.8"/>
        <rect x="9" y="9" width="6" height="6" rx="1" fill="currentColor"/>
      </svg>
    `,
    check: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M5 13l4 4L19 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    `,
    x: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
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
    activeRequests: {},
    debugEnabled: parseStoredBoolean(DEBUG_STORAGE_KEY, false),
    sidebarCollapsed: parseStoredBoolean(SIDEBAR_STORAGE_KEY, false),
    runtimeSummary: "Loading system metadata...",
    requestState: { label: "Ready", tone: "idle" },
    lastUpdated: "No request sent yet.",
    debugFrames: [],
    runtimeSpec: null,
    recoveryNotice: "",
    editingMessageId: null,
    editDraft: "",
  };

  const uiFlags = {
    renderQueued: false,
    focusRenameInput: false,
    focusComposer: false,
    focusEditInput: false,
    feedShouldAutoScroll: true,
  };

  function getActiveRequestForSession(sessionId) {
    if (!sessionId) {
      return null;
    }
    return state.activeRequests[sessionId] || null;
  }

  function registerActiveRequest(sessionId, requestEntry) {
    if (!sessionId) {
      return;
    }
    state.activeRequests[sessionId] = requestEntry;
    if (state.activeSessionId === sessionId) {
      state.isSubmitting = Boolean(requestEntry);
    }
  }

  function clearActiveRequest(sessionId, runId) {
    if (!sessionId) {
      return;
    }
    const existing = getActiveRequestForSession(sessionId);
    if (existing && existing.runId === runId) {
      delete state.activeRequests[sessionId];
    }
    if (state.activeSessionId === sessionId) {
      state.isSubmitting = Boolean(getActiveRequestForSession(sessionId));
    }
  }

  function syncActiveSessionSubmissionState() {
    state.isSubmitting = Boolean(getActiveRequestForSession(state.activeSessionId));
  }

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

  // Root Cause vs Logic: markdown tables using alignment markers (e.g. `---:`)
  // were parsed as plain text because divider detection only accepted hyphens.
  // Accept standard markdown alignment syntax so table rows render as HTML tables.
  function isTableDividerLine(line) {
    return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
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

  function cloneMessage(message, options = {}) {
    return createMessage(message.role === "user" ? "user" : "assistant", String(message.content || ""), {
      id: message.id,
      createdAt: message.createdAt,
      streaming: options.streaming ?? false,
    });
  }

  function cloneMessages(messages, options = {}) {
    const keepStreaming = options.keepStreaming === true;
    if (!Array.isArray(messages)) {
      return [];
    }
    return messages
      .map((entry) => {
        const sanitized = sanitizeMessage(entry);
        if (!sanitized) {
          return null;
        }
        return cloneMessage(sanitized, { streaming: keepStreaming ? Boolean(sanitized.streaming) : false });
      })
      .filter(Boolean);
  }

  function createMessageVersion(userContent, assistantMessageId, options = {}) {
    return {
      id: options.id || createId("ver"),
      userContent: String(userContent || ""),
      assistantMessageId: typeof assistantMessageId === "string" ? assistantMessageId : null,
      createdAt: options.createdAt || new Date().toISOString(),
      status: options.status || "completed",
      snapshotMessages: cloneMessages(options.snapshotMessages || []),
    };
  }

  function sanitizeMessageVersion(raw) {
    if (!raw || typeof raw !== "object") {
      return null;
    }
    return createMessageVersion(String(raw.userContent || ""), typeof raw.assistantMessageId === "string" ? raw.assistantMessageId : null, {
      id: typeof raw.id === "string" ? raw.id : undefined,
      createdAt: typeof raw.createdAt === "string" ? raw.createdAt : undefined,
      status: typeof raw.status === "string" ? raw.status : undefined,
      snapshotMessages: Array.isArray(raw.snapshotMessages) ? raw.snapshotMessages : [],
    });
  }

  function sanitizeMessageVersions(raw) {
    if (!raw || typeof raw !== "object") {
      return {};
    }
    const branches = {};
    Object.entries(raw).forEach(([messageId, branch]) => {
      if (!messageId || !branch || typeof branch !== "object") {
        return;
      }
      const versions = Array.isArray(branch.versions) ? branch.versions.map(sanitizeMessageVersion).filter(Boolean) : [];
      if (!versions.length) {
        return;
      }
      const currentVersionId =
        typeof branch.currentVersionId === "string" && versions.some((entry) => entry.id === branch.currentVersionId)
          ? branch.currentVersionId
          : versions[versions.length - 1].id;
      branches[messageId] = {
        currentVersionId,
        versions,
      };
    });
    return branches;
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
      autoTitle: Boolean(options.autoTitle),
      messages: options.messages?.length ? options.messages : [],
      messageVersions: sanitizeMessageVersions(options.messageVersions),
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

    const baseTitle = typeof raw.title === "string" && raw.title.trim() ? raw.title.trim() : NEW_CHAT_TITLE;
    let messages = Array.isArray(raw.messages) ? raw.messages.map(sanitizeMessage).filter(Boolean) : [];
    if (
      messages.length === 1 &&
      messages[0].role === "assistant" &&
      String(messages[0].content || "").trim() === LEGACY_READY_MESSAGE
    ) {
      messages = [];
    }
    const createdAt = typeof raw.createdAt === "string" ? raw.createdAt : new Date().toISOString();
    const updatedAt = typeof raw.updatedAt === "string" ? raw.updatedAt : createdAt;
    let manualTitle = raw.manualTitle === true;
    const autoTitle = raw.autoTitle === true;
    const backendName =
      typeof raw.backendName === "string"
        ? raw.backendName
        : typeof raw.session_name === "string"
        ? raw.session_name
        : null;
    if (manualTitle && backendName && isAutoOrPlaceholderTitle(baseTitle, messages)) {
      manualTitle = false;
    }
    const title = backendName && !manualTitle ? backendName : baseTitle;

    return createSession(title, {
      id: typeof raw.id === "string" ? raw.id : undefined,
      createdAt,
      updatedAt,
      messages,
      messageVersions: raw.messageVersions,
      backendName,
      manualTitle,
      autoTitle,
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
          createMessage("user", "Can you compare Laminate Bleached Elm with Dark Ash?", {
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
        messageVersionId: typeof parsed.messageVersionId === "string" ? parsed.messageVersionId : null,
        editingMessageId: typeof parsed.editingMessageId === "string" ? parsed.editingMessageId : null,
        runId: typeof parsed.runId === "string" ? parsed.runId : null,
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

  function getSessionById(sessionId) {
    return state.sessions.find((session) => session.id === sessionId) || null;
  }

  function logSessionTitleDebug(event, details = {}, level = "info") {
    const logger =
      level === "warn" && typeof console.warn === "function"
        ? console.warn
        : level === "error" && typeof console.error === "function"
        ? console.error
        : console.info;
    logger(`HTH UI session-title ${event}`, {
      timestamp: new Date().toISOString(),
      ...details,
    });
  }

  function deriveSessionTitleFallback(message) {
    const tokens = String(message || "")
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, MAX_SESSION_NAME_WORDS_FALLBACK);
    if (!tokens.length) {
      return NEW_CHAT_TITLE;
    }
    return tokens.join(" ");
  }

  function findMessageIndex(messages, messageId) {
    return messages.findIndex((entry) => entry.id === messageId);
  }

  function findAssistantAfter(messages, userIndex) {
    if (userIndex < 0 || userIndex + 1 >= messages.length) {
      return null;
    }
    const candidate = messages[userIndex + 1];
    return candidate && candidate.role === "assistant" ? candidate : null;
  }

  function getMessageBranch(session, messageId) {
    if (!session || !messageId || !session.messageVersions || typeof session.messageVersions !== "object") {
      return null;
    }
    const branch = session.messageVersions[messageId];
    if (!branch || !Array.isArray(branch.versions) || !branch.versions.length) {
      return null;
    }
    return branch;
  }

  function ensureBaselineMessageVersion(session, userMessageId) {
    if (!session || !userMessageId) {
      return null;
    }
    if (!session.messageVersions || typeof session.messageVersions !== "object") {
      session.messageVersions = {};
    }
    const existing = getMessageBranch(session, userMessageId);
    if (existing) {
      return existing;
    }

    const index = findMessageIndex(session.messages, userMessageId);
    if (index < 0) {
      return null;
    }
    const message = session.messages[index];
    if (!message || message.role !== "user") {
      return null;
    }
    const assistant = findAssistantAfter(session.messages, index);
    const version = createMessageVersion(message.content, assistant?.id || null, {
      status: "completed",
      snapshotMessages: session.messages,
    });
    const branch = {
      currentVersionId: version.id,
      versions: [version],
    };
    session.messageVersions[userMessageId] = branch;
    return branch;
  }

  function appendMessageVersion(session, userMessageId, userContent, assistantMessageId, status, options = {}) {
    if (!session || !userMessageId) {
      return null;
    }
    if (!session.messageVersions || typeof session.messageVersions !== "object") {
      session.messageVersions = {};
    }
    if (options.ensureBaseline === true) {
      ensureBaselineMessageVersion(session, userMessageId);
    }
    const branch = session.messageVersions[userMessageId] || { currentVersionId: null, versions: [] };
    if (!session.messageVersions[userMessageId]) {
      session.messageVersions[userMessageId] = branch;
    }
    const version = createMessageVersion(userContent, assistantMessageId, {
      status: status || "running",
      snapshotMessages: [],
    });
    branch.versions.push(version);
    branch.currentVersionId = version.id;
    return version;
  }

  function updateMessageVersionSnapshot(session, userMessageId, versionId, status) {
    const branch = getMessageBranch(session, userMessageId);
    if (!branch) {
      return false;
    }
    const version = branch.versions.find((entry) => entry.id === versionId);
    if (!version) {
      return false;
    }
    version.snapshotMessages = cloneMessages(session.messages);
    if (status) {
      version.status = status;
    }
    return true;
  }

  function getMessageVersionState(session, userMessageId) {
    const branch = getMessageBranch(session, userMessageId);
    if (!branch) {
      return {
        hasBranch: false,
        index: 0,
        total: 1,
        canPrev: false,
        canNext: false,
      };
    }
    const activeIndex = Math.max(
      0,
      branch.versions.findIndex((entry) => entry.id === branch.currentVersionId),
    );
    return {
      hasBranch: true,
      index: activeIndex,
      total: branch.versions.length,
      canPrev: activeIndex > 0,
      canNext: activeIndex < branch.versions.length - 1,
    };
  }

  function restoreMessageVersion(session, userMessageId, nextIndex) {
    const branch = getMessageBranch(session, userMessageId);
    if (!branch) {
      return false;
    }
    if (nextIndex < 0 || nextIndex >= branch.versions.length) {
      return false;
    }
    const version = branch.versions[nextIndex];
    if (!version.snapshotMessages.length) {
      return false;
    }
    branch.currentVersionId = version.id;
    session.messages = cloneMessages(version.snapshotMessages);
    return true;
  }

  function finalizeMessageVersion(sessionId, userMessageId, versionId, status) {
    if (!sessionId || !userMessageId || !versionId) {
      return;
    }
    const session = getSessionById(sessionId);
    if (!session) {
      return;
    }
    if (!updateMessageVersionSnapshot(session, userMessageId, versionId, status)) {
      return;
    }
    session.updatedAt = new Date().toISOString();
    state.sessions = sortSessions(state.sessions);
    saveSessions();
  }

  function applyFallbackSessionTitle(sessionId, message, reason) {
    const session = getSessionById(sessionId);
    if (!session) {
      return false;
    }
    if (session.manualTitle) {
      return false;
    }
    const hasBackendName = typeof session.backendName === "string" && session.backendName.trim();
    if (hasBackendName) {
      return false;
    }
    if (session.title !== NEW_CHAT_TITLE && session.title !== UNTITLED_CHAT_TITLE && !session.autoTitle) {
      return false;
    }
    const fallback = deriveSessionTitle(message);
    if (!fallback) {
      return false;
    }
    session.title = fallback;
    session.autoTitle = true;
    session.manualTitle = false;
    session.updatedAt = new Date().toISOString();
    state.sessions = sortSessions(state.sessions);
    saveSessions();
    logSessionTitleDebug("fallback-applied", {
      sessionId,
      fallback,
      reason,
    });
    return true;
  }

  function isAbortError(error) {
    return Boolean(error && typeof error === "object" && "name" in error && error.name === "AbortError");
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
    return deriveSessionTitleFallback(message);
  }

  function getFirstUserMessage(messages) {
    if (!Array.isArray(messages)) {
      return "";
    }
    const firstUser = messages.find((entry) => entry && entry.role === "user");
    return firstUser ? String(firstUser.content || "") : "";
  }

  // Root Cause vs Logic: legacy sessions may mark `manualTitle=true` even when
  // the title is an auto placeholder (e.g. short prefix from first user text).
  // Treat those as non-manual so backend naming can replace them.
  function isAutoOrPlaceholderTitle(title, messages) {
    const normalized = String(title || "").trim();
    if (!normalized) {
      return true;
    }
    if (normalized === NEW_CHAT_TITLE || normalized === UNTITLED_CHAT_TITLE) {
      return true;
    }
    const firstUserMessage = getFirstUserMessage(messages);
    if (!firstUserMessage) {
      return false;
    }
    const derived = deriveSessionTitle(firstUserMessage);
    if (normalized === derived) {
      return true;
    }
    const compactUser = firstUserMessage.replace(/\s+/g, " ").trim();
    if (compactUser && normalized.length <= 6) {
      return normalized.toLowerCase() === compactUser.slice(0, normalized.length).toLowerCase();
    }
    return false;
  }

  // Root Cause vs Logic: the sidebar always rendered the locally derived title,
  // so backend-assigned `session_name` values never appeared even after the
  // naming route finished. Prefer the backend name unless the user manually renamed.
  function getSessionDisplayTitle(session) {
    if (!session) {
      return NEW_CHAT_TITLE;
    }
    if (!session.manualTitle) {
      const backendName = typeof session.backendName === "string" ? session.backendName.trim() : "";
      if (backendName) {
        return backendName;
      }
    }
    return session.title || NEW_CHAT_TITLE;
  }

  function normalizeSessionTitleCandidate(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  function pickSessionTitleCandidate(candidates) {
    for (const candidate of candidates) {
      const normalized = normalizeSessionTitleCandidate(candidate);
      if (normalized) {
        return normalized;
      }
    }
    return "";
  }

  function getBackendSessionTitle(payload) {
    return pickSessionTitleCandidate([
      payload?.session_state?.session_name,
      payload?.session_state?.sessionName,
      payload?.sessionState?.session_name,
      payload?.sessionState?.sessionName,
      payload?.session_name,
      payload?.sessionName,
    ]);
  }

  function applyBackendSessionTitle(sessionId, payload) {
    const backendSessionTitle = getBackendSessionTitle(payload);
    if (!backendSessionTitle) {
      return false;
    }

    const session = state.sessions.find((entry) => entry.id === sessionId);
    if (!session) {
      return false;
    }

    session.backendName = backendSessionTitle;
    if (!session.manualTitle || session.autoTitle || isAutoOrPlaceholderTitle(session.title, session.messages)) {
      session.title = backendSessionTitle;
      session.manualTitle = false;
      session.autoTitle = false;
    }
    session.updatedAt = new Date().toISOString();
    state.sessions = sortSessions(state.sessions);
    saveSessions();
    logSessionTitleDebug("backend-applied", {
      sessionId,
      backendSessionTitle,
    });
    queueRender();
    return true;
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

    const userMessageId = pending.userMessageId || "";
    if (userMessageId) {
      const branch = ensureBaselineMessageVersion(session, userMessageId);
      if (branch && pending.messageVersionId) {
        const existingVersion = branch.versions.find((entry) => entry.id === pending.messageVersionId);
        if (!existingVersion) {
          branch.versions.push(
            createMessageVersion(pending.requestPayload.message || "", pending.assistantMessageId, {
              id: pending.messageVersionId,
              createdAt: pending.startedAt,
              status: "running",
              snapshotMessages: [],
            }),
          );
        }
        branch.currentVersionId = pending.messageVersionId;
      }
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
    const session = createSession(seedTitle || NEW_CHAT_TITLE);
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

  function buildRuntimeDebug(payload) {
    if (payload?.debug) {
      return payload.debug;
    }
    return {
      retrieval: {
        trace_summary: Array.isArray(payload?.tool_trace) ? payload.tool_trace : [],
        thought_blocks: Array.isArray(payload?.thoughts) ? payload.thoughts : [],
        parallel_batches: [],
      },
      grounding: {
        user_impact_limitations: payload?.limitations || [],
      },
    };
  }

  function buildModelMetadata(payload, sessionId) {
    const runtimeDebug = buildRuntimeDebug(payload);
    const traceSummary = Array.isArray(runtimeDebug?.retrieval?.trace_summary) ? runtimeDebug.retrieval.trace_summary : [];
    const thoughtBlocks = Array.isArray(runtimeDebug?.retrieval?.thought_blocks)
      ? runtimeDebug.retrieval.thought_blocks
      : [];
    const limitations = Array.isArray(runtimeDebug?.grounding?.user_impact_limitations)
      ? runtimeDebug.grounding.user_impact_limitations
      : payload?.limitations || [];
    return {
      status: payload?.status || "unknown",
      sessionId,
      toolCalls: traceSummary.length,
      thoughtBlocks: thoughtBlocks.length,
      limitations,
      service: state.runtimeSpec?.server_name || config.serviceName || "HTH MCP",
      serviceVersion: state.runtimeSpec?.server_version || config.serviceVersion || "unknown",
      model: payload?.model || "not_exposed_by_backend",
      debugMode: payload?.debug ? "structured" : "legacy",
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
                const displayTitle = getSessionDisplayTitle(session);
                const escapedDisplayTitle = escapeHtml(displayTitle);
                const collapsedInitial = displayTitle ? displayTitle.slice(0, 1).toUpperCase() : "";
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
                            title="${escapedDisplayTitle}"
                            aria-label="Open ${escapedDisplayTitle}"
                          >
                            <span class="session-title">${state.sidebarCollapsed ? escapeHtml(collapsedInitial) : escapedDisplayTitle}</span>
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

  // Motivation vs Logic: keep prompt version controls inside the message body
  // metadata line so the counter is always visible without chip-style chrome.
  function renderMessageVersionSwitcher(session, message) {
    if (!session || message.role !== "user") {
      return "";
    }
    const versionState = getMessageVersionState(session, message.id);
    const disablePrev = !versionState.hasBranch || !versionState.canPrev || state.isSubmitting;
    const disableNext = !versionState.hasBranch || !versionState.canNext || state.isSubmitting;
    const counter = versionState.hasBranch ? `${versionState.index + 1}/${versionState.total}` : "1/1";
    return `
      <span class="version-switcher" data-role="version-switcher" aria-label="Prompt versions">
        <button
          type="button"
          class="version-nav"
          data-action="version-prev"
          data-message-id="${escapeHtml(message.id)}"
          aria-label="Previous prompt version"
          ${disablePrev ? "disabled" : ""}
        >
          ${icon("chevronLeft")}
        </button>
        <span class="version-count">${counter}</span>
        <button
          type="button"
          class="version-nav"
          data-action="version-next"
          data-message-id="${escapeHtml(message.id)}"
          aria-label="Next prompt version"
          ${disableNext ? "disabled" : ""}
        >
          ${icon("chevronRight")}
        </button>
      </span>
    `;
  }

  function renderUserBubbleContent(message) {
    const isEditing = state.editingMessageId === message.id;
    if (!isEditing) {
      return `<div class="bubble-content" data-role="bubble-content" data-message-id="${escapeHtml(message.id)}">${renderMarkdown(message.content)}</div>`;
    }
    return `
      <div class="message-edit-shell">
        <textarea
          class="message-edit-input"
          data-role="message-edit-input"
          data-message-id="${escapeHtml(message.id)}"
          rows="6"
          aria-label="Edit message"
        >${escapeHtml(state.editDraft)}</textarea>
        <div class="message-edit-actions">
          <button
            type="button"
            class="message-edit-btn primary icon-only"
            data-action="update-message"
            data-message-id="${escapeHtml(message.id)}"
            aria-label="Update message"
            title="Update message"
          >
            ${icon("check")}
          </button>
          <button
            type="button"
            class="message-edit-btn icon-only"
            data-action="cancel-edit-message"
            data-message-id="${escapeHtml(message.id)}"
            aria-label="Cancel editing"
            title="Cancel editing"
          >
            ${icon("x")}
          </button>
        </div>
      </div>
    `;
  }

  function renderMessages() {
    const session = getActiveSession();
    const messages = session?.messages || [];

    return messages
      .map((message, index) => {
        const roleClass = message.role === "user" ? "user" : "assistant";
        const animateClass = index >= messages.length - 2 ? "message-enter" : "";
        const avatar = message.role === "user" ? icon("user") : icon("sparkle");
        const isEditing = message.role === "user" && state.editingMessageId === message.id;
        const versionSwitcher = message.role === "user" ? renderMessageVersionSwitcher(session, message) : "";
        const messageAction =
          message.role === "user"
            ? `
              <button
                type="button"
                class="message-action-btn icon-only"
                data-action="edit-message"
                data-message-id="${escapeHtml(message.id)}"
                aria-label="Edit message"
                title="Edit message"
                ${state.isSubmitting ? "disabled" : ""}
              >
                ${icon("pencil")}
              </button>
            `
            : "";

        return `
          <article
            class="message-row ${roleClass} ${animateClass} ${isEditing ? "message-editing" : ""}"
            data-message-id="${escapeHtml(message.id)}"
            style="--stagger:${Math.max(index - (messages.length - 2), 0) * 34}ms"
          >
            <div class="message-unit ${roleClass}">
              <div class="avatar ${roleClass}">${avatar}</div>
              <div class="bubble ${roleClass}">
                ${message.role === "user" ? renderUserBubbleContent(message) : `<div class="bubble-content" data-role="bubble-content" data-message-id="${escapeHtml(message.id)}">${renderMarkdown(message.content)}</div>`}
                <div class="bubble-meta ${roleClass}">
                  <span class="bubble-role">${message.role === "user" ? "You" : "Claude"}</span>
                  <span class="bubble-meta-right">
                    <span class="bubble-time">${escapeHtml(formatClock(message.createdAt || new Date().toISOString()))}</span>
                    ${message.role === "user" && !isEditing ? versionSwitcher : ""}
                  </span>
                </div>
                ${
                  message.role === "user" && !isEditing
                    ? `
                      <div class="bubble-actions">
                        ${messageAction}
                      </div>
                    `
                    : ""
                }
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
        <p class="prompt-title">Try one of these stock prompts</p>
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
    const activeTitle = getSessionDisplayTitle(activeSession);

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
                <h1>${escapeHtml(activeTitle)}</h1>
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
                    <span class="hint-chip">${icon("keyboardReturn")}Enter for newline</span>
                    <span class="hint-chip">${icon("send")}⌘/Ctrl+Enter send</span>
                    <span class="hint-chip">${icon("panelRight")}⌘/Ctrl+B toggle sidebar</span>
                    <span class="hint-chip">${icon("panelBottom")}⌘/Ctrl+Shift+D debug</span>
                  </div>
                  <div class="composer-actions">
                    ${
                      state.isSubmitting
                        ? `
                          <button
                            type="button"
                            class="send-btn stop-btn icon-only"
                            data-action="stop-message"
                            aria-label="Stop generation"
                            title="Stop generation"
                          >
                            ${icon("stopCircle")}
                          </button>
                        `
                        : `
                          <button
                            type="submit"
                            class="send-btn icon-only"
                            data-action="send-message"
                            aria-label="Send message"
                            title="Send message"
                          >
                            ${icon("send")}
                          </button>
                        `
                    }
                  </div>
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

    const editInput = root.querySelector('[data-role="message-edit-input"]');
    if (editInput) {
      editInput.style.height = "0px";
      editInput.style.height = `${Math.min(220, editInput.scrollHeight)}px`;
      if (uiFlags.focusEditInput) {
        editInput.focus();
        editInput.selectionStart = editInput.value.length;
        editInput.selectionEnd = editInput.value.length;
      }
    }
    uiFlags.focusEditInput = false;

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
    const nextName = state.renameDraft.trim() || UNTITLED_CHAT_TITLE;
    state.renameSessionId = null;
    state.renameDraft = "";
    updateSession(targetSessionId, (session) => ({
      ...session,
      title: nextName,
      manualTitle: true,
      autoTitle: false,
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
    const activeRequest = getActiveRequestForSession(sessionId);
    if (activeRequest) {
      activeRequest.stopRequested = true;
      if (activeRequest.fetchController) {
        activeRequest.fetchController.abort();
      }
      clearActiveRequest(sessionId, activeRequest.runId);
    }

    state.sessions = state.sessions.filter((session) => session.id !== sessionId);

    if (!state.sessions.length) {
      const fallback = createSession(NEW_CHAT_TITLE);
      state.sessions = [fallback];
      state.activeSessionId = fallback.id;
    } else if (state.activeSessionId === sessionId) {
      state.activeSessionId = state.sessions[0].id;
    }

    state.sessions = sortSessions(state.sessions);
    state.renameSessionId = null;
    state.renameDraft = "";
    state.editingMessageId = null;
    state.editDraft = "";
    saveSessions();
    queueRender();
  }

  function resetForNewChat() {
    clearPendingRequest();
    state.renameSessionId = null;
    state.renameDraft = "";
    state.draftMessage = "";
    state.editingMessageId = null;
    state.editDraft = "";
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

  function beginMessageEdit(messageId) {
    if (state.isSubmitting) {
      return;
    }
    const session = getActiveSession();
    if (!session) {
      return;
    }
    const message = session.messages.find((entry) => entry.id === messageId && entry.role === "user");
    if (!message) {
      return;
    }
    ensureBaselineMessageVersion(session, messageId);
    saveSessions();
    state.editingMessageId = messageId;
    state.editDraft = String(message.content || "");
    uiFlags.focusEditInput = true;
    queueRender();
  }

  function cancelMessageEdit() {
    state.editingMessageId = null;
    state.editDraft = "";
    queueRender();
  }

  function switchMessageVersion(messageId, direction) {
    if (state.isSubmitting) {
      return;
    }
    const session = getActiveSession();
    if (!session) {
      return;
    }
    const versionState = getMessageVersionState(session, messageId);
    if (!versionState.hasBranch) {
      return;
    }
    const targetIndex = versionState.index + direction;
    if (!restoreMessageVersion(session, messageId, targetIndex)) {
      return;
    }
    // Root Cause vs Logic: switching prompt versions must rehydrate the exact
    // conversation snapshot for that branch, not only swap one message text.
    state.editingMessageId = null;
    state.editDraft = "";
    session.updatedAt = new Date().toISOString();
    state.sessions = sortSessions(state.sessions);
    saveSessions();
    queueRender();
  }

  function requestStopActiveGeneration() {
    const activeRequest = getActiveRequestForSession(state.activeSessionId);
    if (!activeRequest) {
      return;
    }
    activeRequest.stopRequested = true;
    if (activeRequest.fetchController) {
      activeRequest.fetchController.abort();
    }
    state.requestState = { tone: "idle", label: "Stopped" };
    state.lastUpdated = `Stopped at ${formatClock(new Date())}`;
    console.info("HTH UI stop requested", {
      sessionId: activeRequest.sessionId,
      assistantMessageId: activeRequest.assistantMessageId,
    });
    queueRender();
  }

  function onActionClick(action, target) {
    const sessionId = target.dataset.sessionId;
    const messageId = target.dataset.messageId;
    if (action === "toggle-sidebar") {
      state.sidebarCollapsed = !state.sidebarCollapsed;
      persistUiToggles();
      queueRender();
      return;
    }

    if (action === "new-chat") {
      createAndActivateSession(NEW_CHAT_TITLE);
      resetForNewChat();
      return;
    }

    if (action === "switch-session" && sessionId) {
      state.activeSessionId = sessionId;
      state.renameSessionId = null;
      state.renameDraft = "";
      state.editingMessageId = null;
      state.editDraft = "";
      syncActiveSessionSubmissionState();
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

    if (action === "edit-message" && messageId) {
      beginMessageEdit(messageId);
      return;
    }

    if (action === "cancel-edit-message") {
      cancelMessageEdit();
      return;
    }

    if (action === "update-message") {
      void handleSubmit({ mode: "edit" });
      return;
    }

    if (action === "version-prev" && messageId) {
      switchMessageVersion(messageId, -1);
      return;
    }

    if (action === "version-next" && messageId) {
      switchMessageVersion(messageId, 1);
      return;
    }

    if (action === "stop-message") {
      requestStopActiveGeneration();
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
      return;
    }

    if (role === "message-edit-input") {
      state.editDraft = event.target.value;
      event.target.style.height = "0px";
      event.target.style.height = `${Math.min(220, event.target.scrollHeight)}px`;
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
    if (role === "composer-input" && event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void handleSubmit();
      return;
    }

    if (role === "message-edit-input") {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        void handleSubmit({ mode: "edit" });
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        cancelMessageEdit();
        return;
      }
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
    void handleSubmit();
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
      createAndActivateSession(NEW_CHAT_TITLE);
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

  async function streamAssistantText(sessionId, messageId, text, onProgress, options = {}) {
    const shouldStop = typeof options.shouldStop === "function" ? options.shouldStop : () => false;
    if (!text) {
      if (mutateStreamingMessage(sessionId, messageId, "", false)) {
        saveSessions();
        patchStreamingMessage(messageId, "", false);
        state.sessions = sortSessions(state.sessions);
        queueRender();
      }
      if (onProgress) {
        onProgress("", true, { stopped: false });
      }
      return { content: "", stopped: false };
    }

    let cursor = 0;
    let lastPersist = 0;
    let lastLayoutSync = 0;
    let stopped = false;
    let lastChunk = "";

    while (cursor < text.length) {
      if (shouldStop()) {
        stopped = true;
        break;
      }
      const step = Math.min(text.length - cursor, 2 + Math.floor(Math.random() * 9));
      cursor += step;
      const chunk = text.slice(0, cursor);
      lastChunk = chunk;
      const now = performance.now();

      if (!mutateStreamingMessage(sessionId, messageId, chunk, true)) {
        break;
      }

      patchStreamingMessage(messageId, chunk, true);

      if (now - lastPersist >= STREAM_PERSIST_INTERVAL_MS) {
        saveSessions();
        if (onProgress) {
          onProgress(chunk, false, { stopped: false });
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

    const finalContent = stopped ? lastChunk : text;
    if (mutateStreamingMessage(sessionId, messageId, finalContent, false)) {
      saveSessions();
      patchStreamingMessage(messageId, finalContent, false);
      state.sessions = sortSessions(state.sessions);
      queueRender();
    }
    if (onProgress) {
      onProgress(finalContent, true, { stopped });
    }
    return { content: finalContent, stopped };
  }

  async function submitQuery(requestPayload, options = {}) {
    const response = await fetch(config.queryEndpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      signal: options.signal,
      body: JSON.stringify(requestPayload),
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || "Query request failed.");
    }
    return payload;
  }

  function prepareSubmitContext(options = {}) {
    const mode = options.mode === "edit" ? "edit" : "new";
    const sessionId = state.activeSessionId || createAndActivateSession(NEW_CHAT_TITLE);
    const session = getSessionById(sessionId);
    if (!session) {
      return null;
    }
    const nowIso = new Date().toISOString();

    if (mode === "edit") {
      const editingMessageId = state.editingMessageId;
      const nextMessage = state.editDraft.trim();
      if (!editingMessageId || !nextMessage) {
        return null;
      }
      const userIndex = findMessageIndex(session.messages, editingMessageId);
      if (userIndex < 0) {
        return null;
      }
      const baseUserMessage = session.messages[userIndex];
      if (!baseUserMessage || baseUserMessage.role !== "user") {
        return null;
      }

      const truncated = cloneMessages(session.messages.slice(0, userIndex + 1));
      const updatedUserMessage = cloneMessage(baseUserMessage);
      updatedUserMessage.content = nextMessage;
      truncated[truncated.length - 1] = updatedUserMessage;

      const assistantMessage = createMessage("assistant", "", { createdAt: nowIso, streaming: true });
      session.messages = [...truncated, assistantMessage];
      session.updatedAt = nowIso;
      const version = appendMessageVersion(
        session,
        updatedUserMessage.id,
        nextMessage,
        assistantMessage.id,
        "running",
        { ensureBaseline: true },
      );

      state.editingMessageId = null;
      state.editDraft = "";

      return {
        mode,
        nowIso,
        sessionId,
        message: nextMessage,
        userMessageId: updatedUserMessage.id,
        assistantMessageId: assistantMessage.id,
        messageVersionId: version?.id || null,
      };
    }

    const message = state.draftMessage.trim();
    if (!message) {
      return null;
    }
    const userMessage = createMessage("user", message, { createdAt: nowIso });
    const assistantMessage = createMessage("assistant", "", { createdAt: nowIso, streaming: true });
    session.messages = [...session.messages, userMessage, assistantMessage];
    session.updatedAt = nowIso;
    const version = appendMessageVersion(session, userMessage.id, message, assistantMessage.id, "running");
    state.draftMessage = "";

    return {
      mode,
      nowIso,
      sessionId,
      message,
      userMessageId: userMessage.id,
      assistantMessageId: assistantMessage.id,
      messageVersionId: version?.id || null,
    };
  }

  // Motivation vs Logic: message edits now branch conversation history into
  // versioned snapshots so users can revise any prior prompt, regenerate, and
  // instantly restore the exact state tied to each prompt/response version.
  async function handleSubmit(options = {}) {
    const context = prepareSubmitContext(options);
    if (!context) {
      if (options.mode === "edit") {
        const input = root.querySelector('[data-role="message-edit-input"]');
        if (input) {
          input.focus();
        }
      } else {
        const input = root.querySelector('[data-role="composer-input"]');
        if (input) {
          input.focus();
        }
      }
      return;
    }
    if (getActiveRequestForSession(context.sessionId)) {
      return;
    }

    state.sessions = sortSessions(state.sessions);
    saveSessions();

    const requestPayload = {
      message: context.message,
      sessionId: context.sessionId,
      includeThoughts: true,
      renderMockUi: true,
    };
    const runId = createId("run");
    const fetchController = new AbortController();
    const requestEntry = {
      runId,
      sessionId: context.sessionId,
      userMessageId: context.userMessageId,
      assistantMessageId: context.assistantMessageId,
      messageVersionId: context.messageVersionId,
      stopRequested: false,
      fetchController,
    };
    registerActiveRequest(context.sessionId, requestEntry);

    savePendingRequest({
      runId,
      sessionId: context.sessionId,
      assistantMessageId: context.assistantMessageId,
      userMessageId: context.userMessageId,
      messageVersionId: context.messageVersionId,
      editingMessageId: context.mode === "edit" ? context.userMessageId : null,
      requestPayload,
      startedAt: context.nowIso,
      updatedAt: context.nowIso,
      lastKnownAnswer: "",
    });

    syncActiveSessionSubmissionState();
    state.recoveryNotice = "";
    state.requestState = { tone: "running", label: "Running" };
    state.lastUpdated = `Sent at ${formatClock(context.nowIso)}`;
    uiFlags.feedShouldAutoScroll = true;
    queueRender();

    const startedAt = performance.now();
    pushDebugFrame({
      timestamp: context.nowIso,
      event: "Raw API Request",
      rawApiRequest: requestPayload,
      tokenUsage: {
        prompt: estimateTokens(context.message),
        completion: 0,
        total: estimateTokens(context.message),
        source: "estimated",
      },
      latencyMs: null,
      modelMetadata: {
        service: state.runtimeSpec?.server_name || config.serviceName || "HTH MCP",
        serviceVersion: state.runtimeSpec?.server_version || config.serviceVersion || "unknown",
      },
    });

    try {
      const payload = await submitQuery(requestPayload, { signal: fetchController.signal });
      const answer = payload.answer?.trim() || "No answer returned.";
      const latencyMs = Math.round(performance.now() - startedAt);
      const tokenUsage = extractTokenUsage(payload, requestPayload, answer);
      const modelMetadata = buildModelMetadata(payload, context.sessionId);
      const hasBackendTitle = applyBackendSessionTitle(context.sessionId, payload);
      if (!hasBackendTitle) {
        // Root Cause vs Logic: title fallback should only be used on real errors.
        // Successful responses must rely on backend/LLM naming and log when absent.
        logSessionTitleDebug(
          "backend-missing-on-success",
          {
            sessionId: context.sessionId,
            responseKeys: payload && typeof payload === "object" ? Object.keys(payload) : [],
          },
          "warn",
        );
      }

      pushDebugFrame({
        timestamp: new Date().toISOString(),
        event: "Model Response",
        rawApiRequest: requestPayload,
        debugPayload: buildRuntimeDebug(payload),
        tokenUsage,
        latencyMs,
        modelMetadata,
      });

      const streamResult = await streamAssistantText(
        context.sessionId,
        context.assistantMessageId,
        answer,
        (chunk, done, meta) => {
          updatePendingRequest({
            lastKnownAnswer: chunk,
            completed: done === true,
            stopped: meta?.stopped === true,
          });
        },
        {
          shouldStop: () => Boolean(requestEntry.stopRequested),
        },
      );
      clearPendingRequest();
      finalizeMessageVersion(
        context.sessionId,
        context.userMessageId,
        context.messageVersionId,
        streamResult.stopped ? "stopped" : "completed",
      );
      if (streamResult.stopped) {
        state.requestState = { tone: "idle", label: "Stopped" };
        state.lastUpdated = `Stopped at ${formatClock(new Date())}`;
      } else {
        state.requestState = { tone: payload.status || "answered", label: payload.status || "Answered" };
        state.lastUpdated = `Last response at ${formatClock(new Date())}`;
      }
    } catch (error) {
      const latencyMs = Math.round(performance.now() - startedAt);
      const stoppedByUser = isAbortError(error) && requestEntry.stopRequested;

      if (stoppedByUser) {
        const session = getSessionById(context.sessionId);
        if (session) {
          const assistant = session.messages.find((entry) => entry.id === context.assistantMessageId);
          if (assistant) {
            const existing = String(assistant.content || "").trim();
            assistant.content = existing || "Generation stopped.";
            assistant.streaming = false;
          }
          session.updatedAt = new Date().toISOString();
          state.sessions = sortSessions(state.sessions);
          saveSessions();
        }
        finalizeMessageVersion(context.sessionId, context.userMessageId, context.messageVersionId, "stopped");
        state.requestState = { tone: "idle", label: "Stopped" };
        state.lastUpdated = `Stopped at ${formatClock(new Date())}`;
        clearPendingRequest();
      } else {
        const fallback = "The request failed. Check backend availability and try again.";

        updateSession(context.sessionId, (session) => ({
          ...session,
          updatedAt: new Date().toISOString(),
          messages: session.messages.map((entry) =>
            entry.id === context.assistantMessageId ? { ...entry, content: fallback, streaming: false } : entry,
          ),
        }));
        applyFallbackSessionTitle(context.sessionId, context.message, "query_error");
        finalizeMessageVersion(context.sessionId, context.userMessageId, context.messageVersionId, "error");

        pushDebugFrame({
          timestamp: new Date().toISOString(),
          event: "Error",
          rawApiRequest: requestPayload,
          tokenUsage: {
            prompt: estimateTokens(context.message),
            completion: 0,
            total: estimateTokens(context.message),
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
      }
    } finally {
      clearActiveRequest(context.sessionId, runId);
      queueRender();
    }
  }

  async function recoverPendingRequest(pending) {
    const startedAt = performance.now();
    const activeRequest = getActiveRequestForSession(pending.sessionId);
    const currentRunId = activeRequest?.runId;
    const fetchController = activeRequest?.fetchController;
    try {
      const payload = await submitQuery(pending.requestPayload, { signal: fetchController?.signal });
      const answer = payload.answer?.trim() || "No answer returned.";
      const latencyMs = Math.round(performance.now() - startedAt);
      const tokenUsage = extractTokenUsage(payload, pending.requestPayload, answer);
      const modelMetadata = buildModelMetadata(payload, pending.sessionId);

      const hasBackendTitle = applyBackendSessionTitle(pending.sessionId, payload);
      if (!hasBackendTitle) {
        logSessionTitleDebug(
          "backend-missing-on-recovery-success",
          {
            sessionId: pending.sessionId,
            responseKeys: payload && typeof payload === "object" ? Object.keys(payload) : [],
          },
          "warn",
        );
      }

      pushDebugFrame({
        timestamp: new Date().toISOString(),
        event: "Recovered Response",
        rawApiRequest: pending.requestPayload,
        debugPayload: buildRuntimeDebug(payload),
        tokenUsage,
        latencyMs,
        modelMetadata,
      });

      const streamResult = await streamAssistantText(
        pending.sessionId,
        pending.assistantMessageId,
        answer,
        (chunk, done, meta) => {
          updatePendingRequest({
            lastKnownAnswer: chunk,
            completed: done === true,
            stopped: meta?.stopped === true,
          });
        },
        {
          shouldStop: () =>
            Boolean(activeRequest && activeRequest.runId === currentRunId && activeRequest.stopRequested),
        },
      );
      finalizeMessageVersion(
        pending.sessionId,
        pending.userMessageId,
        pending.messageVersionId,
        streamResult.stopped ? "stopped" : "completed",
      );

      if (streamResult.stopped) {
        state.requestState = { tone: "idle", label: "Stopped" };
        state.lastUpdated = `Stopped at ${formatClock(new Date())}`;
      } else {
        state.requestState = { tone: payload.status || "answered", label: payload.status || "Answered" };
        state.lastUpdated = `Recovered at ${formatClock(new Date())}`;
      }
    } catch (error) {
      const stoppedByUser =
        isAbortError(error) &&
        Boolean(activeRequest && activeRequest.runId === currentRunId && activeRequest.stopRequested);
      if (stoppedByUser) {
        const session = getSessionById(pending.sessionId);
        if (session) {
          const assistant = session.messages.find((entry) => entry.id === pending.assistantMessageId);
          if (assistant) {
            const existing = String(assistant.content || "").trim();
            assistant.content = existing || "Generation stopped.";
            assistant.streaming = false;
          }
          session.updatedAt = new Date().toISOString();
          state.sessions = sortSessions(state.sessions);
          saveSessions();
        }
        finalizeMessageVersion(pending.sessionId, pending.userMessageId, pending.messageVersionId, "stopped");
        state.requestState = { tone: "idle", label: "Stopped" };
        state.lastUpdated = `Stopped at ${formatClock(new Date())}`;
        return;
      }
      const fallback = "The previous response was interrupted after refresh. Retry to continue.";
      mutateStreamingMessage(pending.sessionId, pending.assistantMessageId, fallback, false);
      patchStreamingMessage(pending.assistantMessageId, fallback, false);
      saveSessions();
      state.sessions = sortSessions(state.sessions);
      applyFallbackSessionTitle(pending.sessionId, pending.requestPayload.message, "recovery_error");
      finalizeMessageVersion(pending.sessionId, pending.userMessageId, pending.messageVersionId, "error");
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
      clearActiveRequest(pending.sessionId, currentRunId);
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

    state.editingMessageId = null;
    state.editDraft = "";
    const requestEntry = {
      runId: pending.runId || createId("run"),
      sessionId: pending.sessionId,
      userMessageId: pending.userMessageId,
      assistantMessageId: pending.assistantMessageId,
      messageVersionId: pending.messageVersionId,
      stopRequested: false,
      fetchController: new AbortController(),
    };
    registerActiveRequest(pending.sessionId, requestEntry);
    syncActiveSessionSubmissionState();
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
