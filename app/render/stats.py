from __future__ import annotations

from datetime import datetime
from html import escape

from app.stats.models import UsageEvent, UsageStatsSnapshot, UsageUserGroup


def render_usage_stats_html(snapshot: UsageStatsSnapshot) -> str:
    sections = "\n".join(_render_user_group(group) for group in snapshot.groups)
    if not sections:
        sections = """
        <section class="empty-state">
          <h2>No usage yet</h2>
          <p>Recent query and tool activity will appear here once users start working with the app.</p>
        </section>
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
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <header>
        <h1>User usage stats</h1>
        <p class="intro">Recent user queries and the tool names they triggered, grouped by the best available identity. Only meaningful identity fields are shown here, and tool parameters are intentionally omitted.</p>
        <div class="snapshot-time">Updated {escape(_format_time(snapshot.generated_at))}</div>
      </header>
      <main>{sections}</main>
    </div>
  </body>
</html>"""


def _render_user_group(group: UsageUserGroup) -> str:
    identity_chips: list[str] = []
    if group.user_oid:
        identity_chips.append(f'<span class="chip"><strong>Object ID</strong> {escape(group.user_oid)}</span>')
    if group.user_email:
        identity_chips.append(f'<span class="chip"><strong>Email</strong> {escape(group.user_email)}</span>')
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
        <div class="events">{events}</div>
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
