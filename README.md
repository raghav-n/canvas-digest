# canvas-digest

A daily morning briefing built from your Canvas account. It answers one question: **where do I need to be, what do I need to do, and when? And what's the link to join?**

It produces a single local web page with:

- **Today / next 7 days:** every class, tutorial, test and exam, with venue or Zoom/Teams join link, meeting ID and passcode. It also lists what to prep beforehand.
- **What to do:** tasks and deadlines, sorted by due date and priority
- **Key dates:** exams, presentations and project deadlines for the rest of the semester
- **"Couldn't confirm":** anything the scan couldn't pin down, with a hint on where to check
- Plus deterministic sections pulled straight from the Canvas API: items due in the next 14 days, missing work, calendar events, announcements, unread inbox, recent grades, course scores and updated files. Anything new since yesterday is flagged.

Built for NUS Canvas (Asia/Singapore time, `canvas.nus.edu.sg`), but it works with any Canvas instance after small tweaks. See [Customising](#customising).

## How it works

There are two scripts, run one after the other:

| Script | What it does | Uses an LLM? |
|---|---|---|
| `agent.py` | Runs headless [Claude Code](https://docs.claude.com/en/docs/claude-code) with **read-only** Canvas tools ([canvas-mcp](https://github.com/vishalsachdev/canvas-mcp)). It reads syllabi, pages, modules, announcements, files and inbox messages, then writes a structured brief (`site/data/brief-YYYY-MM-DD.json`). | Yes |
| `digest.py` | Calls the Canvas REST API directly, merges in the latest brief and writes `site/index.html` (plus a dated archive copy). | No |

`digest.py` works without `agent.py`. You still get the API sections, just not the schedule with join links.

## Requirements

- macOS (the scheduler uses `launchd`; the scripts themselves run anywhere)
- Python 3.12+ (standard library only, no `pip install` needed)
- For `agent.py` only:
  - [Claude Code](https://docs.claude.com/en/docs/claude-code), installed and logged in (`claude` on your PATH)
  - [canvas-mcp](https://github.com/vishalsachdev/canvas-mcp): `uv tool install canvas-mcp`

## Setup

### 1. Get a Canvas access token

Canvas → **Account** → **Settings** → **Approved Integrations** → **+ New Access Token**. Copy the token.

### 2. Save your config

```sh
mkdir -p ~/.config/canvas-digest
cat > ~/.config/canvas-digest/env <<'EOF'
CANVAS_API_URL=https://canvas.nus.edu.sg/api/v1
CANVAS_API_TOKEN=paste-your-token-here
EOF
chmod 600 ~/.config/canvas-digest/env
```

You can also set `CANVAS_API_URL` / `CANVAS_API_TOKEN` as environment variables, which take precedence.

### 3. Run it once

```sh
git clone https://github.com/raghav-n/canvas-digest.git
cd canvas-digest
python3 agent.py      # optional, a few minutes; costs Claude usage (capped by --budget)
python3 digest.py
open site/index.html
```

### 4. Schedule it daily (macOS)

```sh
./install.sh            # runs daily at 07:00, serves the page on http://127.0.0.1:8765
./install.sh 06:30 9000 # custom time and port
./install.sh --uninstall
```

This installs two launch agents: one runs `agent.py` then `digest.py` daily (or on wake if the Mac was asleep), and one serves `site/` on localhost only. Logs go to `logs/`.

To trigger a run right now:

```sh
launchctl kickstart gui/$UID/sg.raghavn.canvas-digest
```

## Options

```sh
python3 digest.py [--out DIR] [--now 2026-10-08T07:00:00+08:00]
python3 agent.py  [--out DIR] [--now ISO] [--model sonnet|opus] [--budget 5]
```

| Variable | Default | Purpose |
|---|---|---|
| `CANVAS_AGENT_MODEL` | `sonnet` | Claude model for `agent.py` |
| `CANVAS_MCP_SERVER` | `~/.local/bin/canvas-mcp-server` | Path to the canvas-mcp binary |

`--now` pins the clock, so the same Canvas state gives the same output.

## Customising

- **Timezone:** `TZ` at the top of `digest.py`
- **Look-ahead / look-back windows:** `LOOKAHEAD_DAYS`, `LOOKBACK_DAYS` in `digest.py` and `HORIZON_DAYS` in `agent.py`
- **What the agent looks for:** `agent_prompt.md`. It's written for NUS (it mentions NUS course codes, EduRec/NUSMods and `canvas.nus.edu.sg` URLs). Edit it for your school.
- **Brief structure:** `brief_schema.json`

## Privacy & security

- Your token is read from env or `~/.config/canvas-digest/env` only. `agent.py` passes it to the MCP server via environment, never on the command line or on disk.
- The agent is limited to an allow-list of **read-only** Canvas tools. Posting, replying, marking messages read, downloading files, shell, file writes and web access are explicitly denied.
- Canvas content is treated as untrusted. The prompt tells the model not to follow instructions in it, and all output is HTML-escaped with only `http(s)` links rendered.
- The local server binds to `127.0.0.1` only.
- **`site/` and `logs/` contain your personal data** (grades, inbox excerpts, meeting passcodes). They are git-ignored. Don't publish them.

## Cost

`digest.py` is free. `agent.py` uses Claude and is capped per run by `--budget` (USD, default 5). The cost of each run is printed and stored in the brief's `_meta`.
