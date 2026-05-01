from __future__ import annotations

from datetime import datetime
from html import escape

from app.stats.models import UsageEvent, UsageStatsSnapshot, UsageToolErrorSummary, UsageUserGroup


def render_usage_stats_html(snapshot: UsageStatsSnapshot) -> str:
    # Anonymous users are intentionally hidden from the stats page for now. Keep
    # the model data intact so we can re-enable this section later if needed.
    registered_groups = [group for group in snapshot.groups if group.identity_label != "Anonymous user"]
    sections = "\n".join(_render_user_group(group) for group in registered_groups)
    if not sections:
        sections = """
        <section class="empty-state">
          <h2>No registered usage yet</h2>
          <p>Recent query and tool activity will appear here once registered users start working with the app.</p>
        </section>
        """
    error_sections = _render_tool_errors(snapshot.tool_errors)
    duration_script = """
    <script>
      (function () {
        const canvas = document.getElementById("toolDurationChart");
        const legend = document.getElementById("toolDurationLegend");
        const status = document.getElementById("toolDurationStatus");
        if (!canvas || !legend || !status) {
          return;
        }
        const ctx = canvas.getContext("2d");
        const palette = [
          "#335c81",
          "#f97316",
          "#10b981",
          "#6366f1",
          "#ec4899",
          "#14b8a6",
          "#facc15",
          "#0ea5e9",
        ];
        const colorMap = new Map();
        let colorIndex = 0;

        function colorFor(tool) {
          if (!colorMap.has(tool)) {
            colorMap.set(tool, palette[colorIndex % palette.length]);
            colorIndex += 1;
          }
          return colorMap.get(tool);
        }

        function groupRecords(records) {
          const buckets = new Map();
          records.forEach((record) => {
            if (!record.tool || typeof record.duration_seconds !== "number") {
              return;
            }
            const normalized = record.tool;
            const bucket = buckets.get(normalized) || [];
            bucket.push(record);
            buckets.set(normalized, bucket);
          });
          const series = [];
          buckets.forEach((items, tool) => {
            const sorted = items
              .slice()
              .sort((a, b) => a.recorded_at - b.recorded_at)
              .slice(-40);
            series.push({ tool, points: sorted, color: colorFor(tool) });
          });
          return series;
        }

        function drawChart(series) {
          const width = canvas.width;
          const height = canvas.height;
          const margin = 40;
          ctx.clearRect(0, 0, width, height);
          if (!series.length) {
            ctx.fillStyle = "#64748b";
            ctx.font = "14px Inter, system-ui";
            ctx.fillText("Waiting for tool duration data…", margin, height / 2);
            legend.textContent = "";
            return;
          }
          const allPoints = series.flatMap((entry) => entry.points);
          const minTs = Math.min(...allPoints.map((point) => point.recorded_at));
          const maxTs = Math.max(...allPoints.map((point) => point.recorded_at), minTs + 0.001);
          const maxDuration = Math.max(...allPoints.map((point) => point.duration_seconds), 0.5);
          const plotWidth = width - margin * 2;
          const plotHeight = height - margin * 2;
          ctx.strokeStyle = "#94a3b8";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(margin, margin);
          ctx.lineTo(margin, height - margin);
          ctx.lineTo(width - margin, height - margin);
          ctx.stroke();
          ctx.fillStyle = "#94a3b8";
          ctx.font = "12px Inter, system-ui";
          ctx.fillText("Duration (s)", 8, margin - 6);
          ctx.fillText("Time", width - margin - 28, height - 8);

          series.forEach((entry) => {
            ctx.strokeStyle = entry.color;
            ctx.lineWidth = 2;
            ctx.beginPath();
            entry.points.forEach((point, index) => {
              const xRatio = (point.recorded_at - minTs) / (maxTs - minTs);
              const x = margin + xRatio * plotWidth;
              const yRatio = Math.min(point.duration_seconds / maxDuration, 1);
              const y = height - margin - yRatio * plotHeight;
              if (index === 0) {
                ctx.moveTo(x, y);
              } else {
                ctx.lineTo(x, y);
              }
            });
            ctx.stroke();
            entry.points.forEach((point) => {
              const xRatio = (point.recorded_at - minTs) / (maxTs - minTs);
              const x = margin + xRatio * plotWidth;
              const yRatio = Math.min(point.duration_seconds / maxDuration, 1);
              const y = height - margin - yRatio * plotHeight;
              ctx.fillStyle = entry.color;
              ctx.beginPath();
              ctx.arc(x, y, 3, 0, Math.PI * 2);
              ctx.fill();
            });
          });

          legend.innerHTML = "";
          series.forEach((entry) => {
            const swatch = document.createElement("span");
            swatch.innerHTML =
              '<span class="swatch" style="background:' +
              entry.color +
              '"></span>' +
              entry.tool;
            legend.appendChild(swatch);
          });
        }

        async function fetchData() {
          status.textContent = "Updating…";
          try {
            const response = await fetch("durations?_=" + Date.now(), { cache: "no-store" });
            if (!response.ok) {
              throw new Error("Failed to refresh");
            }
            const payload = await response.json();
            const series = groupRecords(payload.records || []);
            drawChart(series);
            const generated = payload.generated_at ? new Date(payload.generated_at * 1000) : new Date();
            status.textContent = "Last refreshed " + generated.toLocaleTimeString();
          } catch (error) {
            status.textContent = "Update failed";
            console.error("Tool duration refresh failed", error);
          }
        }

        fetchData();
        setInterval(fetchData, 30000);
      })();
    </script>
    """

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>User usage stats</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f6f7f9;
        --panel: #ffffff;
        --line: #d8dde3;
        --text: #17212b;
        --muted: #5e6b78;
        --accent: #335c81;
        --accent-soft: #edf4fa;
        --chip: #eef2f6;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: var(--bg);
        color: var(--text);
      }}
      .wrap {{
        max-width: 1100px;
        margin: 0 auto;
        padding: 32px 20px 48px;
      }}
      header {{
        display: grid;
        gap: 10px;
        margin-bottom: 24px;
      }}
      h1 {{
        margin: 0;
        font-size: 28px;
        line-height: 1.15;
        letter-spacing: 0;
      }}
      .intro {{
        margin: 0;
        color: var(--muted);
        max-width: 72ch;
      }}
      .snapshot-time {{
        color: var(--muted);
        font-size: 14px;
      }}
      .group {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 10px;
        padding: 20px;
        margin-bottom: 16px;
      }}
      .group-head {{
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: flex-start;
        margin-bottom: 16px;
      }}
      .group-title {{
        margin: 0 0 8px;
        font-size: 20px;
        line-height: 1.2;
      }}
      .chips {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }}
      .chip {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 5px 10px;
        border-radius: 999px;
        background: var(--chip);
        color: var(--text);
        font-size: 13px;
        line-height: 1.1;
        border: 1px solid transparent;
        max-width: 100%;
      }}
      .chip--muted {{
        color: var(--muted);
      }}
      .chip--access {{
        background: #e8f3ec;
        border-color: #c8dfd0;
      }}
      .chip--tool {{
        background: #f5efe3;
        border-color: #e5d2ad;
        font-weight: 600;
      }}
      .chip--error {{
        background: #fcebea;
        border-color: #efb6b2;
        color: #7f1d1d;
      }}
      .chip--ai-claude {{
        background: #f3e8dd;
        border-color: #dec1a6;
      }}
      .chip--ai-openai {{
        background: #e5f4ef;
        border-color: #b8ded0;
      }}
      .chip--ai-chatgpt {{
        background: #e3f2fd;
        border-color: #b7d7f4;
      }}
      .chip--ai-codex {{
        background: #ede7f6;
        border-color: #cabce5;
      }}
      .chip--ai-cursor {{
        background: #eceff8;
        border-color: #c3c9e6;
      }}
      .chip--ai-other {{
        background: #eef2f6;
        border-color: #d8dde3;
      }}
      .summary-grid {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
        gap: 16px;
        margin: 14px 0 18px;
      }}
      .summary-card {{
        border: 1px solid var(--line);
        border-radius: 10px;
        padding: 14px;
        background: #fbfcfd;
        display: grid;
        gap: 10px;
      }}
      .summary-title {{
        margin: 0;
        color: var(--accent);
        font-size: 14px;
        font-weight: 700;
      }}
      .tool-list {{
        display: grid;
        gap: 10px;
      }}
      .tool-row {{
        display: grid;
        gap: 8px;
      }}
      .tool-row + .tool-row {{
        border-top: 1px solid var(--line);
        padding-top: 10px;
      }}
      .tool-clients {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }}
      .events {{
        display: grid;
        gap: 12px;
      }}
      .event {{
        border-top: 1px solid var(--line);
        padding-top: 12px;
        display: grid;
        gap: 8px;
      }}
      .event:first-child {{
        border-top: 0;
        padding-top: 0;
      }}
      .event-row {{
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: baseline;
        flex-wrap: wrap;
      }}
      .event-kind {{
        margin: 0;
        font-weight: 600;
        color: var(--accent);
      }}
      .event-time {{
        color: var(--muted);
        font-size: 13px;
      }}
      .event-query {{
        margin: 0;
        font-size: 15px;
        line-height: 1.45;
      }}
      .event-tools {{
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        color: var(--muted);
        font-size: 13px;
      }}
      .error-section {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 10px;
        padding: 20px;
        margin-top: 18px;
      }}
      .error-list {{
        display: grid;
        gap: 12px;
      }}
      .error-card {{
        border-top: 1px solid var(--line);
        padding-top: 12px;
        display: grid;
        gap: 8px;
      }}
      .error-card:first-child {{
        border-top: 0;
        padding-top: 0;
      }}
      .error-request {{
        margin: 0;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
        font-size: 13px;
        overflow-wrap: anywhere;
      }}
      .empty-state {{
        background: var(--panel);
        border: 1px dashed var(--line);
        border-radius: 10px;
        padding: 28px 20px;
        color: var(--muted);
      }}
      .empty-state h2 {{
        margin: 0 0 8px;
        color: var(--text);
        font-size: 18px;
      }}
      .duration-section {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 10px;
        padding: 20px;
        margin-bottom: 16px;
      }}
      .duration-section h2 {{
        margin: 0 0 6px;
      }}
      .duration-section canvas {{
        width: 100%;
        max-width: 860px;
        height: 280px;
        display: block;
        margin: 0 auto 16px;
      }}
      .duration-legend {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        font-size: 13px;
        margin-bottom: 8px;
      }}
      .duration-legend span {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }}
      .duration-legend .swatch {{
        width: 14px;
        height: 14px;
        border-radius: 2px;
      }}
      .duration-status {{
        font-size: 13px;
        color: var(--muted);
      }}
      @media (max-width: 640px) {{
        .wrap {{
          padding: 20px 14px 36px;
        }}
        .group {{
          padding: 16px;
        }}
        .group-head {{
          flex-direction: column;
        }}
        .summary-grid {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <header>
        <h1>User usage stats</h1>
        <p class="intro">Recent user queries and tool usage, grouped by user identity. Access groups are narrowed to configured OAuth/plugin groups only, client apps are stacked by AI family, and tool parameters are intentionally omitted.</p>
        <div class="snapshot-time">Updated {escape(_format_time(snapshot.generated_at))}</div>
      </header>
      <main>
        {sections}
        <section class="duration-section">
          <div class="group-head">
            <div>
              <h2 class="group-title">Tool response durations</h2>
              <p class="intro">Each line tracks a tool’s end-to-end response time. The chart refreshes every 30 seconds.</p>
            </div>
          </div>
          <canvas id="toolDurationChart" width="860" height="280"></canvas>
          <div id="toolDurationLegend" class="duration-legend"></div>
          <div id="toolDurationStatus" class="duration-status">Waiting for data…</div>
        </section>
        {error_sections}
    </main>
    </div>
      {duration_script}
  </body>
</html>"""


def _render_user_group(group: UsageUserGroup) -> str:
    identity_chips: list[str] = []
    if group.tenant_id:
        identity_chips.append(f'<span class="chip"><strong>Tenant</strong> {escape(group.tenant_id)}</span>')
    if group.user_oid:
        identity_chips.append(f'<span class="chip"><strong>Object ID</strong> {escape(group.user_oid)}</span>')
    if group.identity_key:
        identity_chips.append(f'<span class="chip"><strong>Identity key</strong> {escape(group.identity_key)}</span>')
    if group.user_email:
        identity_chips.append(f'<span class="chip"><strong>Email</strong> {escape(group.user_email)}</span>')
    if group.roles:
        identity_chips.append(f'<span class="chip"><strong>Roles</strong> {escape(", ".join(group.roles))}</span>')
    if group.matched_groups:
        for matched_group in group.matched_groups:
            identity_chips.append(f'<span class="chip chip--access"><strong>Access group</strong> {escape(matched_group)}</span>')
    else:
        identity_chips.append('<span class="chip chip--muted">No configured OAuth/PL group matched</span>')
    if not identity_chips:
        identity_chips.append('<span class="chip chip--muted">Anonymous</span>')

    events = "\n".join(_render_event(event) for event in group.events[:5])
    return f"""
      <article class="group">
        <div class="group-head">
          <div>
            <h2 class="group-title">{escape(group.identity_label)}</h2>
            <div class="chips">{''.join(identity_chips)}</div>
          </div>
        </div>
        <div class="summary-grid">
          {_render_client_summary(group)}
          {_render_tool_summary(group)}
        </div>
        <div class="events">{events}</div>
      </article>
    """


def _render_client_summary(group: UsageUserGroup) -> str:
    if not group.clients:
        chips = '<span class="chip chip--muted">No AI client recorded</span>'
    else:
        chips = "".join(
            f'<span class="chip {_ai_chip_class(client.ai_key)}">{escape(_friendly_client_label(client.label))}'
            f' <strong>{client.count}</strong></span>'
            for client in group.clients
        )
    return f"""
      <section class="summary-card">
        <p class="summary-title">AI clients</p>
        <div class="chips">{chips}</div>
      </section>
    """


def _render_tool_summary(group: UsageUserGroup) -> str:
    if not group.tools:
        body = '<span class="chip chip--muted">No tool usage recorded</span>'
    else:
        rows = []
        for tool in group.tools:
            client_chips = "".join(
                f'<span class="chip {_ai_chip_class(client.ai_key)}">{escape(_friendly_client_label(client.label))}'
                f' <strong>{client.count}</strong></span>'
                for client in tool.clients
            )
            rows.append(
                f"""
                <div class="tool-row">
                  <div class="chips"><span class="chip chip--tool">{escape(tool.name)} <strong>{tool.count}</strong></span></div>
                  <div class="tool-clients">{client_chips}</div>
                </div>
                """
            )
        body = f'<div class="tool-list">{"".join(rows)}</div>'
    return f"""
      <section class="summary-card">
        <p class="summary-title">Tool used</p>
        {body}
      </section>
    """


def _render_tool_errors(errors: list[UsageToolErrorSummary]) -> str:
    if not errors:
        return """
          <section class="error-section">
            <div class="group-head">
              <div>
                <h2 class="group-title">Tool call errors</h2>
                <p class="intro">No tool call errors have been recorded.</p>
              </div>
            </div>
          </section>
        """

    cards = "\n".join(_render_tool_error(error) for error in errors)
    return f"""
      <section class="error-section">
        <div class="group-head">
          <div>
            <h2 class="group-title">Tool call errors</h2>
            <p class="intro">Recent tool failures include the user query or upstream request that triggered the error.</p>
          </div>
        </div>
        <div class="error-list">{cards}</div>
      </section>
    """


def _render_tool_error(error: UsageToolErrorSummary) -> str:
    status = f"HTTP {error.error_status_code}" if error.error_status_code is not None else "Error"
    query = error.query or "No user query recorded."
    request = error.error_request or error.query or "No request recorded."
    message = error.error_message or "No error message recorded."
    return f"""
      <article class="error-card">
        <div class="event-row">
          <div class="chips">
            <span class="chip chip--error">{escape(status)}</span>
            <span class="chip chip--tool">{escape(error.tool_name)}</span>
            <span class="chip {_ai_chip_class(error.ai_key)}">{escape(error.client_label)}</span>
          </div>
          <div class="event-time">{escape(_format_time(error.recorded_at))}</div>
        </div>
        <p class="event-query"><strong>User/query</strong> {escape(_truncate_query(query, limit=260))}</p>
        <p class="error-request"><strong>Triggered</strong> {escape(request)}</p>
        <p class="event-query"><strong>Error</strong> {escape(_truncate_query(message, limit=320))}</p>
      </article>
    """


def _render_event(event: UsageEvent) -> str:
    timestamp = escape(_format_time(event.recorded_at))
    if event.kind == "tool":
        tool_label = ", ".join(escape(tool_name) for tool_name in event.tool_names) or "Tool request"
        return f"""
          <section class="event">
            <div class="event-row">
              <p class="event-kind">Tool used</p>
              <div class="event-time">{timestamp}</div>
            </div>
            <p class="event-query">{tool_label}</p>
          </section>
        """

    tools = "".join(f'<span class="chip">{escape(tool_name)}</span>' for tool_name in event.tool_names)
    tool_block = (
        f'<div class="event-tools"><span>Tools used</span><div class="chips">{tools}</div></div>'
        if tools
        else '<div class="event-tools"><span>No tool name recorded</span></div>'
    )
    query = escape(_truncate_query(event.query or ""))
    return f"""
      <section class="event">
        <div class="event-row">
          <p class="event-kind">Query</p>
          <div class="event-time">{timestamp}</div>
        </div>
        <p class="event-query">{query}</p>
        {tool_block}
      </section>
    """


def _truncate_query(value: str, limit: int = 180) -> str:
    rendered = " ".join(value.split())
    if len(rendered) <= limit:
        return rendered
    return f"{rendered[: max(0, limit - 1)].rstrip()}…"


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%d %b %Y, %H:%M")


def _ai_chip_class(ai_key: str) -> str:
    allowed = {"claude", "openai", "chatgpt", "codex", "cursor", "other"}
    key = ai_key if ai_key in allowed else "other"
    return f"chip--ai-{key}"


def _friendly_client_label(label: str | None) -> str:
    if not label:
        return "Unknown AI"
    parts = label.split(" (", 1)
    return parts[0]
