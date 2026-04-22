import React, { useEffect, useMemo, useRef, useState } from "https://esm.sh/react@18.3.1";
import { createRoot } from "https://esm.sh/react-dom@18.3.1/client";
import { AnimatePresence, motion } from "https://esm.sh/framer-motion@11.13.1?external=react,react-dom";
import {
  Bug,
  ChevronLeft,
  ChevronRight,
  Loader2,
  Pencil,
  Plus,
  SendHorizontal,
  Sparkles,
  Trash2,
  UserRound
} from "https://esm.sh/lucide-react@0.475.0?external=react";
const configNode = document.getElementById("mock-ui-config");
const config = configNode ? JSON.parse(configNode.textContent || "{}") : {};
const SESSION_STORAGE_KEY = "hth-claude-desktop-sessions-v2";
const DEBUG_STORAGE_KEY = "hth-claude-debug-mode-v2";
const SIDEBAR_STORAGE_KEY = "hth-claude-sidebar-collapsed-v2";
const GROUP_ORDER = ["Today", "Yesterday", "Last 7 Days", "Earlier"];
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
      "'": "&#39;"
    }[char];
  });
}
function renderMarkdown(value) {
  if (value === void 0 || value === null) {
    return "";
  }
  const escaped = escapeHtml(value);
  return escaped.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/\*(.+?)\*/g, "<em>$1</em>").replace(/\n/g, "<br />");
}
function formatClock(timestamp) {
  return new Date(timestamp).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit"
  });
}
function estimateTokens(text) {
  return Math.max(1, Math.ceil(String(text || "").length / 4));
}
function getSessionGroup(dateValue) {
  const now = /* @__PURE__ */ new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const targetDate = new Date(dateValue);
  const target = new Date(targetDate.getFullYear(), targetDate.getMonth(), targetDate.getDate());
  const diffDays = Math.floor((today - target) / 864e5);
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
    Earlier: []
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
    createdAt: options.createdAt || (/* @__PURE__ */ new Date()).toISOString(),
    streaming: Boolean(options.streaming)
  };
}
function createSession(title, options = {}) {
  const createdAt = options.createdAt || (/* @__PURE__ */ new Date()).toISOString();
  const assistantGreeting = createMessage(
    "assistant",
    "I\u2019m ready. Ask about stock, compare variants, or inspect the current session with grounded tool calls.",
    { createdAt }
  );
  return {
    id: options.id || createId("session"),
    title,
    createdAt,
    updatedAt: options.updatedAt || createdAt,
    messages: options.messages?.length ? options.messages : [assistantGreeting]
  };
}
function sanitizeMessage(raw) {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const role = raw.role === "user" ? "user" : "assistant";
  const content = typeof raw.content === "string" ? raw.content : "";
  return createMessage(role, content, {
    id: typeof raw.id === "string" ? raw.id : void 0,
    createdAt: typeof raw.createdAt === "string" ? raw.createdAt : void 0,
    streaming: false
  });
}
function sanitizeSession(raw) {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const title = typeof raw.title === "string" && raw.title.trim() ? raw.title.trim() : "New chat";
  const messages = Array.isArray(raw.messages) ? raw.messages.map(sanitizeMessage).filter(Boolean) : [];
  const createdAt = typeof raw.createdAt === "string" ? raw.createdAt : (/* @__PURE__ */ new Date()).toISOString();
  const updatedAt = typeof raw.updatedAt === "string" ? raw.updatedAt : createdAt;
  return createSession(title, {
    id: typeof raw.id === "string" ? raw.id : void 0,
    createdAt,
    updatedAt,
    messages
  });
}
function buildSeedSessions() {
  const now = Date.now();
  return sortSessions([
    createSession("Laminate variants for venue", {
      createdAt: new Date(now - 45 * 6e4).toISOString(),
      updatedAt: new Date(now - 12 * 6e4).toISOString(),
      messages: [
        createMessage(
          "assistant",
          "Welcome to the desktop simulation. We can dig into live tool traces and normalize variant evidence as we go.",
          { createdAt: new Date(now - 45 * 6e4).toISOString() }
        ),
        createMessage("user", "Can you compare fl-la-la-lam-1-ble with fl-da-dan?", {
          createdAt: new Date(now - 20 * 6e4).toISOString()
        }),
        createMessage(
          "assistant",
          "Yes. Share any pricing or stock thresholds and I\u2019ll include them in the comparison logic.",
          { createdAt: new Date(now - 19 * 6e4).toISOString() }
        )
      ]
    }),
    createSession("Supplier weather impact notes", {
      createdAt: new Date(now - 27 * 36e5).toISOString(),
      updatedAt: new Date(now - 26 * 36e5).toISOString()
    }),
    createSession("Currency sanity check", {
      createdAt: new Date(now - 3 * 864e5).toISOString(),
      updatedAt: new Date(now - 3 * 864e5 + 18 * 6e4).toISOString()
    })
  ]);
}
function deriveSessionTitle(message) {
  const compact = message.replace(/\s+/g, " ").trim();
  if (!compact) {
    return "New chat";
  }
  return compact.length > 42 ? `${compact.slice(0, 42)}\u2026` : compact;
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
function parseStoredBoolean(storageKey, defaultValue) {
  try {
    const value = window.localStorage.getItem(storageKey);
    if (value === null) {
      return defaultValue;
    }
    return value === "true";
  } catch (_error) {
    return defaultValue;
  }
}
function pushLimited(frames, nextFrame, limit = 80) {
  return [nextFrame, ...frames].slice(0, limit);
}
function extractTokenUsage(payload, requestPayload, answer) {
  const usage = payload?.token_usage || payload?.usage || {};
  const hasPayloadTokens = usage.prompt_tokens !== void 0 || usage.input_tokens !== void 0 || usage.completion_tokens !== void 0 || usage.output_tokens !== void 0 || usage.total_tokens !== void 0;
  const promptTokens = hasPayloadTokens ? usage.prompt_tokens ?? usage.input_tokens ?? estimateTokens(JSON.stringify(requestPayload)) : estimateTokens(JSON.stringify(requestPayload));
  const completionTokens = hasPayloadTokens ? usage.completion_tokens ?? usage.output_tokens ?? estimateTokens(answer) : estimateTokens(answer);
  return {
    prompt: Number(promptTokens),
    completion: Number(completionTokens),
    total: Number(usage.total_tokens ?? promptTokens + completionTokens),
    source: hasPayloadTokens ? "payload" : "estimated"
  };
}
function buildModelMetadata(payload, sessionId, runtimeSpec) {
  return {
    status: payload?.status || "unknown",
    sessionId,
    toolCalls: Array.isArray(payload?.tool_trace) ? payload.tool_trace.length : 0,
    thoughtBlocks: Array.isArray(payload?.thoughts) ? payload.thoughts.length : 0,
    limitations: payload?.limitations || [],
    service: runtimeSpec?.server_name || config.serviceName || "HTH MCP",
    serviceVersion: runtimeSpec?.server_version || config.serviceVersion || "unknown",
    model: payload?.model || "not_exposed_by_backend"
  };
}
async function streamText(text, onChunk) {
  if (!text) {
    onChunk("");
    return;
  }
  let cursor = 0;
  while (cursor < text.length) {
    const stride = Math.min(text.length - cursor, 1 + Math.floor(Math.random() * 5));
    cursor += stride;
    onChunk(text.slice(0, cursor));
    await new Promise((resolve) => window.setTimeout(resolve, 16));
  }
}
async function fetchSystemSpec() {
  const response = await fetch(config.systemSpecEndpoint);
  if (!response.ok) {
    throw new Error("Unable to fetch system metadata.");
  }
  return response.json();
}
async function submitQuery(requestPayload) {
  const response = await fetch(config.queryEndpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(requestPayload)
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "Query request failed.");
  }
  return payload;
}
function StatusPill({ tone, label }) {
  return /* @__PURE__ */ React.createElement("span", { className: `status-pill ${tone}` }, label);
}
function MessageBubble({ message, index }) {
  const isUser = message.role === "user";
  return /* @__PURE__ */ React.createElement(
    motion.article,
    {
      layout: true,
      initial: { opacity: 0, y: 14 },
      animate: { opacity: 1, y: 0 },
      exit: { opacity: 0, y: -10 },
      transition: { duration: 0.2, ease: "easeOut", delay: index * 0.02 },
      className: `flex w-full ${isUser ? "justify-end" : "justify-start"}`
    },
    /* @__PURE__ */ React.createElement("div", { className: `flex max-w-[82%] gap-3 ${isUser ? "flex-row-reverse" : ""}` }, /* @__PURE__ */ React.createElement(
      "div",
      {
        className: `mt-1 grid h-8 w-8 shrink-0 place-items-center rounded-full border ${isUser ? "border-claude-line bg-claude-soft text-claude-ink" : "border-amber-300/50 bg-amber-100/70 text-amber-700"}`
      },
      isUser ? /* @__PURE__ */ React.createElement(UserRound, { size: 16 }) : /* @__PURE__ */ React.createElement(Sparkles, { size: 16 })
    ), /* @__PURE__ */ React.createElement(
      "div",
      {
        className: `rounded-2xl border px-4 py-3 text-[15px] leading-7 tracking-[0.002em] ${isUser ? "border-[#d7d3c8] bg-[#f2efe8] text-claude-ink" : "border-[#ece7da] bg-claude-paper text-[#2f2b24]"}`
      },
      /* @__PURE__ */ React.createElement("div", { dangerouslySetInnerHTML: { __html: renderMarkdown(message.content) } }),
      message.streaming ? /* @__PURE__ */ React.createElement("span", { className: "ml-1 inline-block h-4 w-[2px] animate-pulse rounded bg-claude-muted align-middle" }) : null
    ))
  );
}
function SessionSkeleton() {
  return /* @__PURE__ */ React.createElement("div", { className: "space-y-3 pt-1" }, Array.from({ length: 9 }).map((_, index) => /* @__PURE__ */ React.createElement("div", { key: `skeleton-${index}`, className: "skeleton-row h-9 rounded-xl" })));
}
function App() {
  const [sessions, setSessions] = useState([]);
  const [activeSessionId, setActiveSessionId] = useState(null);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [renameSessionId, setRenameSessionId] = useState(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [draftMessage, setDraftMessage] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [debugFrames, setDebugFrames] = useState([]);
  const [debugEnabled, setDebugEnabled] = useState(() => parseStoredBoolean(DEBUG_STORAGE_KEY, false));
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => parseStoredBoolean(SIDEBAR_STORAGE_KEY, false));
  const [runtimeSpec, setRuntimeSpec] = useState(null);
  const [requestState, setRequestState] = useState({ tone: "idle", label: "Ready" });
  const [lastUpdated, setLastUpdated] = useState("No request sent yet.");
  const feedRef = useRef(null);
  const textareaRef = useRef(null);
  const activeSession = useMemo(
    () => sessions.find((session) => session.id === activeSessionId) || null,
    [sessions, activeSessionId]
  );
  const groupedSessions = useMemo(() => groupSessions(sessions), [sessions]);
  const runtimeSummary = useMemo(() => {
    const scope = Array.isArray(runtimeSpec?.scope) ? runtimeSpec.scope.slice(0, 2).join(" \xB7 ") : "";
    return scope || "Simulation ready with live MCP query routing.";
  }, [runtimeSpec]);
  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(() => {
      if (cancelled) {
        return;
      }
      const restored = parseStoredSessions();
      setSessions(restored);
      setActiveSessionId(restored[0]?.id || null);
      setLoadingSessions(false);
    }, 680);
    fetchSystemSpec().then(setRuntimeSpec).catch(() => setRuntimeSpec(null));
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);
  useEffect(() => {
    if (!loadingSessions) {
      window.localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(sessions));
    }
  }, [sessions, loadingSessions]);
  useEffect(() => {
    window.localStorage.setItem(DEBUG_STORAGE_KEY, String(debugEnabled));
  }, [debugEnabled]);
  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarCollapsed));
  }, [sidebarCollapsed]);
  useEffect(() => {
    if (!activeSessionId && sessions.length) {
      setActiveSessionId(sessions[0].id);
    }
  }, [activeSessionId, sessions]);
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) {
      return;
    }
    textarea.style.height = "0px";
    const nextHeight = Math.min(220, textarea.scrollHeight);
    textarea.style.height = `${nextHeight}px`;
  }, [draftMessage]);
  useEffect(() => {
    const feed = feedRef.current;
    if (feed) {
      feed.scrollTop = feed.scrollHeight;
    }
  }, [activeSession?.messages]);
  function updateSession(sessionId, updater) {
    setSessions(
      (prev) => sortSessions(
        prev.map((session) => {
          if (session.id !== sessionId) {
            return session;
          }
          return updater(session);
        })
      )
    );
  }
  function createAndActivateSession(seedTitle = "New chat") {
    const session = createSession(seedTitle);
    setSessions((prev) => sortSessions([session, ...prev]));
    setActiveSessionId(session.id);
    return session.id;
  }
  function handleNewChat() {
    setRenameSessionId(null);
    setRenameDraft("");
    createAndActivateSession("New chat");
    setDraftMessage("");
    setRequestState({ tone: "idle", label: "Ready" });
    setLastUpdated("No request sent yet.");
  }
  function handleDeleteSession(sessionId) {
    setSessions((prev) => {
      const next = prev.filter((session) => session.id !== sessionId);
      if (!next.length) {
        const fallback = createSession("New chat");
        setActiveSessionId(fallback.id);
        return [fallback];
      }
      if (activeSessionId === sessionId) {
        setActiveSessionId(next[0].id);
      }
      return sortSessions(next);
    });
    setRenameSessionId(null);
    setRenameDraft("");
  }
  function beginRename(session) {
    setRenameSessionId(session.id);
    setRenameDraft(session.title);
  }
  function commitRename() {
    if (!renameSessionId) {
      return;
    }
    const nextName = renameDraft.trim() || "Untitled chat";
    updateSession(renameSessionId, (session) => ({
      ...session,
      title: nextName,
      updatedAt: (/* @__PURE__ */ new Date()).toISOString()
    }));
    setRenameSessionId(null);
    setRenameDraft("");
  }
  async function handleSubmit(event) {
    event.preventDefault();
    if (isSubmitting) {
      return;
    }
    const message = draftMessage.trim();
    if (!message) {
      textareaRef.current?.focus();
      return;
    }
    const currentSessionId = activeSession?.id || createAndActivateSession("New chat");
    const nowIso = (/* @__PURE__ */ new Date()).toISOString();
    const userMessage = createMessage("user", message, { createdAt: nowIso });
    const assistantMessage = createMessage("assistant", "", {
      createdAt: nowIso,
      streaming: true
    });
    updateSession(currentSessionId, (session) => ({
      ...session,
      title: session.title === "New chat" || session.title === "Untitled chat" ? deriveSessionTitle(message) : session.title,
      updatedAt: nowIso,
      messages: [...session.messages, userMessage, assistantMessage]
    }));
    const requestPayload = {
      message,
      sessionId: currentSessionId,
      includeThoughts: true,
      renderMockUi: true
    };
    setDraftMessage("");
    setIsSubmitting(true);
    setRequestState({ tone: "running", label: "Running" });
    setLastUpdated(`Sent at ${formatClock(nowIso)}`);
    const startedAt = performance.now();
    const requestFrame = {
      timestamp: nowIso,
      event: "Raw API Request",
      rawApiRequest: requestPayload,
      tokenUsage: {
        prompt: estimateTokens(message),
        completion: 0,
        total: estimateTokens(message),
        source: "estimated"
      },
      latencyMs: null,
      modelMetadata: {
        service: runtimeSpec?.server_name || config.serviceName || "HTH MCP",
        serviceVersion: runtimeSpec?.server_version || config.serviceVersion || "unknown"
      }
    };
    setDebugFrames((prev) => pushLimited(prev, requestFrame));
    try {
      const payload = await submitQuery(requestPayload);
      const answer = payload.answer?.trim() || "No answer returned.";
      const latencyMs = Math.round(performance.now() - startedAt);
      const tokenUsage = extractTokenUsage(payload, requestPayload, answer);
      const modelMetadata = buildModelMetadata(payload, currentSessionId, runtimeSpec);
      const responseFrame = {
        timestamp: (/* @__PURE__ */ new Date()).toISOString(),
        event: "Model Response",
        rawApiRequest: requestPayload,
        tokenUsage,
        latencyMs,
        modelMetadata
      };
      setDebugFrames((prev) => pushLimited(prev, responseFrame));
      await streamText(answer, (chunk) => {
        updateSession(currentSessionId, (session) => ({
          ...session,
          updatedAt: (/* @__PURE__ */ new Date()).toISOString(),
          messages: session.messages.map(
            (existing) => existing.id === assistantMessage.id ? { ...existing, content: chunk, streaming: true } : existing
          )
        }));
      });
      updateSession(currentSessionId, (session) => ({
        ...session,
        updatedAt: (/* @__PURE__ */ new Date()).toISOString(),
        messages: session.messages.map(
          (existing) => existing.id === assistantMessage.id ? { ...existing, streaming: false } : existing
        )
      }));
      setRequestState({ tone: payload.status || "answered", label: payload.status || "Answered" });
      setLastUpdated(`Last response at ${formatClock(/* @__PURE__ */ new Date())}`);
    } catch (error) {
      const latencyMs = Math.round(performance.now() - startedAt);
      const fallback = "The request failed. Check backend availability and try again.";
      updateSession(currentSessionId, (session) => ({
        ...session,
        updatedAt: (/* @__PURE__ */ new Date()).toISOString(),
        messages: session.messages.map(
          (existing) => existing.id === assistantMessage.id ? { ...existing, content: fallback, streaming: false } : existing
        )
      }));
      const errorFrame = {
        timestamp: (/* @__PURE__ */ new Date()).toISOString(),
        event: "Error",
        rawApiRequest: requestPayload,
        tokenUsage: {
          prompt: estimateTokens(message),
          completion: 0,
          total: estimateTokens(message),
          source: "estimated"
        },
        latencyMs,
        modelMetadata: {
          status: "error",
          detail: error instanceof Error ? error.message : "Unknown error",
          service: runtimeSpec?.server_name || config.serviceName || "HTH MCP",
          serviceVersion: runtimeSpec?.server_version || config.serviceVersion || "unknown"
        }
      };
      setDebugFrames((prev) => pushLimited(prev, errorFrame));
      setRequestState({ tone: "error", label: "Error" });
      setLastUpdated(`Request failed at ${formatClock(/* @__PURE__ */ new Date())}`);
    } finally {
      setIsSubmitting(false);
    }
  }
  function handleComposerKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!isSubmitting) {
        handleSubmit(event);
      }
    }
  }
  return /* @__PURE__ */ React.createElement("div", { className: "h-screen overflow-hidden bg-[radial-gradient(circle_at_top_left,rgba(222,208,186,0.38),transparent_40%),linear-gradient(180deg,#fcfbf8_0%,#f7f6f3_100%)]" }, /* @__PURE__ */ React.createElement(
    "div",
    {
      className: "grid h-full overflow-hidden",
      style: {
        gridTemplateColumns: `${sidebarCollapsed ? 84 : 286}px minmax(0, 1fr)`
      }
    },
    /* @__PURE__ */ React.createElement(
      motion.aside,
      {
        initial: false,
        animate: { opacity: 1, x: 0 },
        transition: { duration: 0.2, ease: "easeOut" },
        className: "h-full border-r border-claude-line/90 bg-claude-paper/70 px-3 py-4 backdrop-blur-md"
      },
      /* @__PURE__ */ React.createElement("div", { className: "flex h-full flex-col" }, /* @__PURE__ */ React.createElement("div", { className: "mb-4 flex items-center justify-between gap-2 px-2" }, !sidebarCollapsed ? /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "text-[11px] font-semibold uppercase tracking-[0.16em] text-claude-muted" }, "Claude Desktop"), /* @__PURE__ */ React.createElement("p", { className: "text-[13px] font-medium text-claude-ink" }, config.serviceName || "HTH MCP")) : null, /* @__PURE__ */ React.createElement(
        motion.button,
        {
          type: "button",
          whileHover: { scale: 1.06, y: -1 },
          whileTap: { scale: 0.96 },
          transition: { type: "spring", stiffness: 300, damping: 22 },
          className: "grid h-8 w-8 place-items-center rounded-lg border border-claude-line bg-claude-paper text-claude-muted",
          onClick: () => setSidebarCollapsed((prev) => !prev),
          "aria-label": sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"
        },
        sidebarCollapsed ? /* @__PURE__ */ React.createElement(ChevronRight, { size: 16 }) : /* @__PURE__ */ React.createElement(ChevronLeft, { size: 16 })
      )), /* @__PURE__ */ React.createElement(
        motion.button,
        {
          type: "button",
          whileHover: { scale: 1.02, y: -1 },
          whileTap: { scale: 0.98 },
          transition: { type: "spring", stiffness: 320, damping: 22 },
          onClick: handleNewChat,
          className: `mx-2 inline-flex items-center gap-2 rounded-xl border border-claude-line bg-claude-paper px-3 py-2.5 text-sm font-semibold text-claude-ink shadow-sm ${sidebarCollapsed ? "justify-center" : "justify-start"}`
        },
        /* @__PURE__ */ React.createElement(Plus, { size: 16 }),
        !sidebarCollapsed ? /* @__PURE__ */ React.createElement("span", null, "New Chat") : null
      ), /* @__PURE__ */ React.createElement("div", { className: "thin-scrollbar mt-4 min-h-0 flex-1 overflow-y-auto px-1 pb-3" }, loadingSessions ? /* @__PURE__ */ React.createElement(SessionSkeleton, null) : GROUP_ORDER.map((groupName) => {
        const entries = groupedSessions[groupName];
        if (!entries.length) {
          return null;
        }
        return /* @__PURE__ */ React.createElement("section", { key: groupName, className: "mb-4" }, !sidebarCollapsed ? /* @__PURE__ */ React.createElement("p", { className: "mb-2 px-3 text-[11px] font-semibold uppercase tracking-[0.14em] text-claude-muted" }, groupName) : null, /* @__PURE__ */ React.createElement("div", { className: "space-y-1.5" }, entries.map((session) => {
          const isActive = session.id === activeSessionId;
          const isRenaming = renameSessionId === session.id && !sidebarCollapsed;
          return /* @__PURE__ */ React.createElement(
            motion.div,
            {
              layout: true,
              key: session.id,
              initial: { opacity: 0, y: 8 },
              animate: { opacity: 1, y: 0 },
              className: `session-item group mx-1 flex items-center gap-2 rounded-xl border px-2 py-1.5 ${isActive ? "border-[#d8d1c3] bg-[#f1ede5]" : "border-transparent bg-transparent hover:border-[#e7e2d8] hover:bg-[#f8f6f1]"}`
            },
            isRenaming ? /* @__PURE__ */ React.createElement(
              "input",
              {
                autoFocus: true,
                value: renameDraft,
                onChange: (event) => setRenameDraft(event.target.value),
                onBlur: commitRename,
                onKeyDown: (event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    commitRename();
                  }
                  if (event.key === "Escape") {
                    setRenameSessionId(null);
                    setRenameDraft("");
                  }
                },
                className: "w-full rounded-lg border border-claude-line bg-white px-2 py-1 text-sm outline-none ring-0 focus:border-[#cbc2b2]"
              }
            ) : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(
              "button",
              {
                type: "button",
                onClick: () => {
                  setActiveSessionId(session.id);
                  setRenameSessionId(null);
                  setRenameDraft("");
                },
                className: `min-w-0 flex-1 truncate text-left text-sm ${sidebarCollapsed ? "text-center" : ""}`,
                title: session.title
              },
              sidebarCollapsed ? session.title.slice(0, 1).toUpperCase() : session.title
            ), !sidebarCollapsed ? /* @__PURE__ */ React.createElement("div", { className: "session-actions ml-auto flex items-center gap-1" }, /* @__PURE__ */ React.createElement(
              "button",
              {
                type: "button",
                onClick: () => beginRename(session),
                className: "grid h-7 w-7 place-items-center rounded-md text-claude-muted hover:bg-claude-soft hover:text-claude-ink",
                "aria-label": `Rename ${session.title}`
              },
              /* @__PURE__ */ React.createElement(Pencil, { size: 14 })
            ), /* @__PURE__ */ React.createElement(
              "button",
              {
                type: "button",
                onClick: () => handleDeleteSession(session.id),
                className: "grid h-7 w-7 place-items-center rounded-md text-claude-muted hover:bg-claude-soft hover:text-[#8d493d]",
                "aria-label": `Delete ${session.title}`
              },
              /* @__PURE__ */ React.createElement(Trash2, { size: 14 })
            )) : null)
          );
        })));
      })), /* @__PURE__ */ React.createElement("div", { className: `mt-auto border-t border-claude-line/80 pt-4 ${sidebarCollapsed ? "px-1" : "px-2"}` }, !sidebarCollapsed ? /* @__PURE__ */ React.createElement("div", { className: "mb-3 rounded-xl border border-claude-line bg-claude-paper px-3 py-2 text-xs leading-5 text-claude-muted" }, /* @__PURE__ */ React.createElement("p", { className: "font-semibold text-claude-ink" }, "Runtime"), /* @__PURE__ */ React.createElement("p", null, runtimeSummary)) : null, /* @__PURE__ */ React.createElement(
        "label",
        {
          className: `flex cursor-pointer items-center gap-2 text-sm text-claude-ink ${sidebarCollapsed ? "justify-center" : ""}`
        },
        /* @__PURE__ */ React.createElement(
          "input",
          {
            type: "checkbox",
            className: "h-4 w-4 rounded border border-[#c9c2b5] accent-[#6f5f4b]",
            checked: debugEnabled,
            onChange: (event) => setDebugEnabled(event.target.checked)
          }
        ),
        !sidebarCollapsed ? /* @__PURE__ */ React.createElement("span", null, "Enable Debug Mode") : /* @__PURE__ */ React.createElement(Bug, { size: 14 })
      )))
    ),
    /* @__PURE__ */ React.createElement("main", { className: "min-w-0" }, /* @__PURE__ */ React.createElement("div", { className: "flex h-full flex-col" }, /* @__PURE__ */ React.createElement(
      motion.section,
      {
        initial: false,
        animate: { height: debugEnabled ? "75vh" : "100vh" },
        transition: { duration: 0.2, ease: "easeOut" },
        className: "min-h-0"
      },
      /* @__PURE__ */ React.createElement("section", { className: "flex h-full min-h-0 flex-col" }, /* @__PURE__ */ React.createElement("header", { className: "border-b border-claude-line/70 px-8 py-5" }, /* @__PURE__ */ React.createElement("div", { className: "mx-auto flex w-full max-w-5xl items-start justify-between gap-4" }, /* @__PURE__ */ React.createElement("div", { className: "min-w-0" }, /* @__PURE__ */ React.createElement("p", { className: "text-[11px] font-semibold uppercase tracking-[0.15em] text-claude-muted" }, "Unified Conversation"), /* @__PURE__ */ React.createElement("h1", { className: "truncate text-lg font-semibold text-claude-ink" }, activeSession?.title || "New chat")), /* @__PURE__ */ React.createElement("div", { className: "flex shrink-0 items-center gap-3" }, /* @__PURE__ */ React.createElement(StatusPill, { tone: requestState.tone, label: requestState.label }), /* @__PURE__ */ React.createElement("p", { className: "hidden text-xs text-claude-muted lg:block" }, lastUpdated)))), /* @__PURE__ */ React.createElement("div", { ref: feedRef, className: "thin-scrollbar min-h-0 flex-1 overflow-y-auto px-8 py-8" }, /* @__PURE__ */ React.createElement("div", { className: "mx-auto flex w-full max-w-5xl flex-col gap-4" }, /* @__PURE__ */ React.createElement(AnimatePresence, { initial: false }, (activeSession?.messages || []).map((message, index) => /* @__PURE__ */ React.createElement(MessageBubble, { key: message.id, message, index }))))), /* @__PURE__ */ React.createElement("div", { className: "border-t border-claude-line/70 px-8 pb-7 pt-4" }, /* @__PURE__ */ React.createElement("form", { onSubmit: handleSubmit, className: "mx-auto w-full max-w-5xl" }, /* @__PURE__ */ React.createElement(
        motion.div,
        {
          layout: true,
          transition: { type: "spring", stiffness: 260, damping: 26 },
          className: "rounded-2xl border border-[#dbd5c8] bg-claude-paper p-3 shadow-composer"
        },
        /* @__PURE__ */ React.createElement(
          "textarea",
          {
            ref: textareaRef,
            value: draftMessage,
            rows: 1,
            onChange: (event) => setDraftMessage(event.target.value),
            onKeyDown: handleComposerKeyDown,
            placeholder: "Message Claude",
            className: "thin-scrollbar max-h-[220px] min-h-[56px] w-full resize-none bg-transparent px-1 py-2 text-[15px] leading-7 text-claude-ink outline-none placeholder:text-[#9a9589]"
          }
        ),
        /* @__PURE__ */ React.createElement("div", { className: "mt-2 flex items-center justify-between gap-3" }, /* @__PURE__ */ React.createElement("p", { className: "text-xs text-claude-muted" }, lastUpdated), /* @__PURE__ */ React.createElement(
          motion.button,
          {
            type: "submit",
            whileHover: { scale: 1.03, y: -1 },
            whileTap: { scale: 0.97 },
            transition: { type: "spring", stiffness: 320, damping: 22 },
            disabled: isSubmitting,
            className: "inline-flex items-center gap-2 rounded-xl bg-[#2f2b24] px-4 py-2 text-sm font-semibold text-white disabled:cursor-wait disabled:opacity-75"
          },
          isSubmitting ? /* @__PURE__ */ React.createElement(Loader2, { size: 16, className: "animate-spin" }) : /* @__PURE__ */ React.createElement(SendHorizontal, { size: 16 }),
          /* @__PURE__ */ React.createElement("span", null, isSubmitting ? "Sending" : "Send")
        ))
      ))))
    ), /* @__PURE__ */ React.createElement(AnimatePresence, null, debugEnabled ? /* @__PURE__ */ React.createElement(
      motion.section,
      {
        key: "debug-drawer",
        initial: { height: 0, opacity: 0, y: 28 },
        animate: { height: "25vh", opacity: 1, y: 0 },
        exit: { height: 0, opacity: 0, y: 28 },
        transition: { duration: 0.2, ease: "easeOut" },
        className: "border-t border-claude-line bg-claude-paper/95"
      },
      /* @__PURE__ */ React.createElement("div", { className: "h-full px-6 py-4" }, /* @__PURE__ */ React.createElement("div", { className: "mx-auto flex h-full w-full max-w-6xl flex-col" }, /* @__PURE__ */ React.createElement("div", { className: "mb-3 flex items-center justify-between gap-4" }, /* @__PURE__ */ React.createElement("div", { className: "flex items-center gap-2 text-sm font-semibold text-claude-ink" }, /* @__PURE__ */ React.createElement(Bug, { size: 16 }), /* @__PURE__ */ React.createElement("span", null, "System Debug Stream")), /* @__PURE__ */ React.createElement("p", { className: "text-xs text-claude-muted" }, config.queryEndpoint || "/api/v1/query", " \xB7 live JSON frames")), /* @__PURE__ */ React.createElement("div", { className: "thin-scrollbar min-h-0 flex-1 space-y-3 overflow-y-auto pr-1" }, !debugFrames.length ? /* @__PURE__ */ React.createElement("div", { className: "rounded-xl border border-dashed border-claude-line bg-[#faf8f3] px-4 py-5 text-sm text-claude-muted" }, "Run a message to stream Raw API Request, Token Usage, Latency (ms), and Model Metadata.") : debugFrames.map((frame, index) => /* @__PURE__ */ React.createElement(
        motion.article,
        {
          key: `${frame.timestamp}-${index}`,
          initial: { opacity: 0, y: 8 },
          animate: { opacity: 1, y: 0 },
          className: "rounded-xl border border-[#ded8cc] bg-[#f8f5ee] p-3"
        },
        /* @__PURE__ */ React.createElement("div", { className: "mb-2 flex items-center justify-between text-xs uppercase tracking-[0.12em] text-claude-muted" }, /* @__PURE__ */ React.createElement("span", null, frame.event), /* @__PURE__ */ React.createElement("span", null, formatClock(frame.timestamp))),
        /* @__PURE__ */ React.createElement("pre", { className: "debug-json" }, JSON.stringify(frame, null, 2))
      )))))
    ) : null)))
  ));
}
const rootElement = document.getElementById("root");
if (rootElement) {
  createRoot(rootElement).render(/* @__PURE__ */ React.createElement(App, null));
}
