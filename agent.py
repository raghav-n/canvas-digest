#!/usr/bin/env python3
"""LLM pass over Canvas: where to be, what to do, when, and join links.

Runs headless Claude Code with read-only Canvas MCP tools and a JSON schema,
then writes site/data/brief-YYYY-MM-DD.json for digest.py to render.

Usage:
  python3 agent.py [--out DIR] [--now ISO] [--model opus] [--budget 5]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from digest import DEFAULT_OUT, TZ, load_config

HERE = Path(__file__).resolve().parent
HORIZON_DAYS = 14
CANVAS_SERVER = os.environ.get("CANVAS_MCP_SERVER",
                               str(Path.home() / ".local/bin/canvas-mcp-server"))

READ_ONLY_TOOLS = [
    "list_courses", "get_course_details", "get_course_structure", "get_course_content_overview",
    "get_syllabus", "get_front_page", "list_pages", "get_page_content", "get_page_details",
    "list_modules", "list_module_items", "list_announcements", "list_discussion_topics",
    "get_discussion_topic_details", "list_discussion_entries", "get_discussion_entry_details",
    "get_discussion_with_replies", "list_assignments", "get_assignment_details",
    "get_my_upcoming_assignments", "get_my_todo_items", "get_my_peer_reviews_todo",
    "get_my_submission_status", "get_my_submission", "list_course_files", "read_course_file",
    "list_conversations", "get_conversation_details", "get_my_enrollments", "get_my_profile",
    "get_unread_count",
]
# Everything that writes to Canvas or the local machine stays off.
DENIED = ["mcp__canvas__post_discussion_entry", "mcp__canvas__reply_to_discussion_entry",
          "mcp__canvas__mark_conversations_read", "mcp__canvas__download_course_file",
          "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task"]


def run(now: dt.datetime, model: str | None, budget: float) -> dict:
    base, token = load_config()
    prompt = (HERE / "agent_prompt.md").read_text().format(
        today=now.strftime("%Y-%m-%d"), weekday=now.strftime("%A"), now=now.strftime("%H:%M"),
        horizon_end=(now + dt.timedelta(days=HORIZON_DAYS)).strftime("%Y-%m-%d"))
    schema = (HERE / "brief_schema.json").read_text()
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")

    with tempfile.TemporaryDirectory() as tmp:
        # Token goes via env (inherited by the stdio server), never on argv or disk.
        mcp = Path(tmp) / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"canvas": {
            "type": "stdio", "command": CANVAS_SERVER, "args": ["--role", "student"]}}}))
        cmd = [claude, "-p", prompt, "--output-format", "json", "--json-schema", schema,
               "--strict-mcp-config", "--mcp-config", str(mcp),
               "--allowedTools", *[f"mcp__canvas__{t}" for t in READ_ONLY_TOOLS],
               "--disallowedTools", *DENIED,
               "--no-session-persistence", "--max-budget-usd", str(budget)]
        if model:
            cmd += ["--model", model]
        env = dict(os.environ, CANVAS_API_URL=base, CANVAS_API_TOKEN=token)
        p = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, timeout=1800)

    if p.returncode != 0:
        sys.exit(f"claude exited {p.returncode}: {p.stderr[-2000:] or p.stdout[-2000:]}")
    res = json.loads(p.stdout)
    brief = res.get("structured_output")
    if brief is None:  # older CLIs put it in "result" as text
        brief = json.loads(res.get("result") or "null")
    if not isinstance(brief, dict) or "sessions" not in brief:
        sys.exit(f"no structured output: {p.stdout[-2000:]}")
    brief["_meta"] = dict(generated_at=now.isoformat(), cost_usd=res.get("total_cost_usd"),
                          turns=res.get("num_turns"), duration_ms=res.get("duration_ms"))
    return brief


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--now")
    ap.add_argument("--model", default=os.environ.get("CANVAS_AGENT_MODEL", "sonnet"))
    ap.add_argument("--budget", type=float, default=5.0)
    a = ap.parse_args()
    now = (dt.datetime.fromisoformat(a.now).astimezone(TZ) if a.now
           else dt.datetime.now(TZ).replace(microsecond=0))
    brief = run(now, a.model, a.budget)
    data_dir = a.out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    out = data_dir / f"brief-{now:%Y-%m-%d}.json"
    out.write_text(json.dumps(brief, indent=1, ensure_ascii=False))
    m = brief["_meta"]
    print(f"{now.isoformat()} wrote {out} (sessions={len(brief['sessions'])} "
          f"tasks={len(brief['tasks'])} gaps={len(brief['gaps'])} "
          f"cost=${m['cost_usd'] or 0:.2f} turns={m['turns']})")


if __name__ == "__main__":
    main()
