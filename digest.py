#!/usr/bin/env python3
"""Deterministic daily Canvas digest.

Calls the Canvas REST API directly (no LLM involved), writes a self-contained
HTML page plus a JSON snapshot. Same Canvas state + same --now => same output.

Config (first match wins):
  env vars CANVAS_API_URL / CANVAS_API_TOKEN
  ~/.config/canvas-digest/env   (KEY=VALUE lines, chmod 600)

Usage:
  python3 digest.py [--out DIR] [--now 2026-10-08T07:00:00+08:00]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Singapore")
LOOKAHEAD_DAYS = 14
LOOKBACK_DAYS = 7
CONFIG_FILE = Path.home() / ".config" / "canvas-digest" / "env"
DEFAULT_OUT = Path(__file__).resolve().parent / "site"


# ---------------------------------------------------------------- config / http

def load_config() -> tuple[str, str]:
    cfg = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    url = os.environ.get("CANVAS_API_URL") or cfg.get("CANVAS_API_URL")
    token = os.environ.get("CANVAS_API_TOKEN") or cfg.get("CANVAS_API_TOKEN")
    if not url or not token:
        sys.exit(f"Missing CANVAS_API_URL / CANVAS_API_TOKEN (env or {CONFIG_FILE})")
    return url.rstrip("/"), token


class Canvas:
    def __init__(self, base: str, token: str):
        self.base, self.token = base, token

    def _get(self, url: str):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()), r.headers.get("Link", "")

    def get(self, path: str, params: list[tuple[str, str]] | None = None, paginate=True):
        """GET with Link-header pagination. Lists are concatenated."""
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data, link = self._get(url)
        if not paginate or not isinstance(data, list):
            return data
        out = list(data)
        pages = 1
        while (m := re.search(r'<([^>]+)>;\s*rel="next"', link)) and pages < 50:
            data, link = self._get(m.group(1))
            out.extend(data)
            pages += 1
        return out


# ---------------------------------------------------------------- helpers

def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)


def iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, d):
        self.parts.append(d)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "li", "div", "h1", "h2", "h3", "tr"):
            self.parts.append(" ")


def html_to_text(s: str | None, limit=320) -> str:
    if not s:
        return ""
    p = _Text()
    p.feed(s)
    text = re.sub(r"\s+", " ", "".join(p.parts)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def short_code(course: dict) -> str:
    return (course.get("course_code") or course.get("name") or str(course["id"])).split(" ")[0]


# ---------------------------------------------------------------- collection

def collect(c: Canvas, now: dt.datetime) -> dict:
    errors: dict[str, str] = {}

    def safe(key, fn, default):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            errors[key] = f"HTTP {e.code}"
        except Exception as e:  # network etc. -- keep going, show on page
            errors[key] = f"{type(e).__name__}: {e}"
        return default

    profile = c.get("/users/self/profile", paginate=False)  # fail hard on auth errors

    courses = safe("courses", lambda: c.get("/courses", [
        ("enrollment_state", "active"), ("include[]", "total_scores"),
        ("include[]", "term"), ("per_page", "100")]), [])
    courses = sorted(courses, key=lambda x: (short_code(x), x["id"]))
    by_id = {x["id"]: x for x in courses}

    start, end = now - dt.timedelta(days=LOOKBACK_DAYS), now + dt.timedelta(days=LOOKAHEAD_DAYS)

    planner = safe("planner", lambda: c.get("/planner/items", [
        ("start_date", iso(now - dt.timedelta(days=1))), ("end_date", iso(end)),
        ("per_page", "100")]), [])

    missing = safe("missing", lambda: c.get("/users/self/missing_submissions", [
        ("include[]", "course"), ("filter[]", "submittable"), ("per_page", "100")]), [])

    ctx = [("context_codes[]", f"course_{cid}") for cid in by_id]
    announcements = safe("announcements", lambda: c.get("/announcements", ctx + [
        ("start_date", iso(start)), ("end_date", iso(now)), ("per_page", "100")]) if ctx else [], [])

    unread_convos = safe("inbox", lambda: c.get("/conversations", [
        ("scope", "unread"), ("per_page", "50")]), [])

    notices = safe("notices", lambda: c.get("/accounts/self/account_notifications", [
        ("per_page", "50")]), [])

    graded, files = [], []
    for cid, course in by_id.items():
        graded += [dict(s, _course=cid) for s in safe(f"graded:{short_code(course)}", lambda: c.get(
            f"/courses/{cid}/students/submissions", [
                ("student_ids[]", "self"), ("graded_since", iso(start)),
                ("include[]", "assignment"), ("per_page", "100")]), [])]
        # Files tab is often disabled for students; a 401/403 here is normal, so not an error.
        try:
            fs = c.get(f"/courses/{cid}/files", [
                ("sort", "updated_at"), ("order", "desc"), ("per_page", "30")], paginate=False)
            files += [dict(f, _course=cid) for f in fs
                      if (t := parse_ts(f.get("updated_at"))) and t >= start]
        except urllib.error.HTTPError:
            pass

    return dict(profile=profile, courses=courses, planner=planner, missing=missing,
                announcements=announcements, inbox=unread_convos, graded=graded,
                files=files, notices=notices, errors=errors)


# ---------------------------------------------------------------- shaping

def build(raw: dict, now: dt.datetime, prev: dict | None) -> dict:
    by_id = {x["id"]: x for x in raw["courses"]}
    code = lambda cid: short_code(by_id[cid]) if cid in by_id else ""
    absurl = lambda u: urllib.parse.urljoin(raw["base_html"] + "/", u) if u else ""
    prev_at = parse_ts(prev["generated_at"]) if prev else now - dt.timedelta(days=1)
    today = now.date()

    # Due items (planner), excluding announcements / pure calendar events
    due, events = [], []
    for it in raw["planner"]:
        p = it.get("plannable") or {}
        kind = it.get("plannable_type")
        when = parse_ts(p.get("due_at") or p.get("todo_date") or it.get("plannable_date"))
        if when is None:
            continue
        sub = it.get("submissions") or {}
        row = dict(
            kind=kind, title=p.get("title") or p.get("name") or "(untitled)",
            course=code(it.get("course_id")) or it.get("context_name") or "", when=when.isoformat(),
            url=absurl(it.get("html_url")), points=p.get("points_possible"),
            submitted=bool(sub.get("submitted")), graded=bool(sub.get("graded")),
            late=bool(sub.get("late")), missing=bool(sub.get("missing")),
            feedback=bool(sub.get("has_feedback")), new_activity=bool(it.get("new_activity")),
            done=bool((it.get("planner_override") or {}).get("marked_complete")))
        if kind == "calendar_event":
            events.append(row)
        elif kind != "announcement" and when >= now - dt.timedelta(hours=12):
            due.append(row)
    due.sort(key=lambda r: (r["when"], r["title"]))
    events.sort(key=lambda r: (r["when"], r["title"]))
    events = [e for e in events if parse_ts(e["when"]).date() >= today]

    overdue = sorted(({
        "title": m.get("name") or "(untitled)", "course": code(m.get("course_id")),
        "when": (parse_ts(m.get("due_at")) or now).isoformat(), "url": absurl(m.get("html_url")),
        "points": m.get("points_possible")} for m in raw["missing"]),
        key=lambda r: (r["when"], r["title"]))

    anns = []
    for a in raw["announcements"]:
        cid = int(str(a.get("context_code", "course_0")).split("_")[-1])
        posted = parse_ts(a.get("posted_at") or a.get("created_at"))
        anns.append(dict(title=a.get("title") or "", course=code(cid), when=posted.isoformat(),
                         url=absurl(a.get("html_url")), excerpt=html_to_text(a.get("message")),
                         is_new=posted > prev_at, unread=a.get("read_state") == "unread"))
    anns.sort(key=lambda r: (r["when"], r["title"]), reverse=True)

    inbox = sorted(({
        "subject": cv.get("subject") or "(no subject)", "course": cv.get("context_name") or "",
        "when": (parse_ts(cv.get("last_message_at")) or now).isoformat(),
        "from": ", ".join(sorted(p["name"] for p in cv.get("participants", [])
                                 if p.get("id") != raw["profile"].get("id"))),
        "excerpt": html_to_text(cv.get("last_message"), 200)} for cv in raw["inbox"]),
        key=lambda r: r["when"], reverse=True)

    grades = []
    prev_grades = {g["course_id"]: g for g in (prev or {}).get("grades", [])}
    for cid, course in by_id.items():
        enr = next((e for e in course.get("enrollments", []) if e.get("type") == "student"), {})
        score = enr.get("computed_current_score")
        letter = enr.get("computed_current_grade")
        old = prev_grades.get(cid, {}).get("score")
        grades.append(dict(course_id=cid, course=code(cid), name=course.get("name", ""),
                           score=score, letter=letter,
                           delta=(round(score - old, 2) if score is not None and old is not None
                                  and score != old else None)))

    graded = sorted(({
        "title": (s.get("assignment") or {}).get("name") or "(assignment)",
        "course": code(s["_course"]), "when": (parse_ts(s.get("graded_at")) or now).isoformat(),
        "score": s.get("score"), "grade": s.get("grade"),
        "points": (s.get("assignment") or {}).get("points_possible"),
        "url": absurl((s.get("assignment") or {}).get("html_url")),
        "is_new": (parse_ts(s.get("graded_at")) or now) > prev_at}
        for s in raw["graded"] if s.get("graded_at") and s.get("workflow_state") == "graded"),
        key=lambda r: r["when"], reverse=True)

    files = sorted(({
        "name": f.get("display_name") or f.get("filename") or "", "course": code(f["_course"]),
        "when": parse_ts(f.get("updated_at")).isoformat(), "url": f.get("url") and
        f"{raw['base_html']}/courses/{f['_course']}/files/{f['id']}" or "",
        "is_new": parse_ts(f.get("updated_at")) > prev_at} for f in raw["files"]),
        key=lambda r: (r["when"], r["name"]), reverse=True)

    notices = sorted(({
        "title": n.get("subject") or "", "excerpt": html_to_text(n.get("message")),
        "when": (parse_ts(n.get("start_at")) or now).isoformat()} for n in raw["notices"]),
        key=lambda r: (r["when"], r["title"]), reverse=True)

    return dict(generated_at=now.isoformat(), notices=notices, user=raw["profile"].get("name", ""),
                due=due, overdue=overdue, events=events, announcements=anns, inbox=inbox,
                grades=grades, graded=graded, files=files, errors=raw["errors"],
                prev_generated_at=prev["generated_at"] if prev else None)


# ---------------------------------------------------------------- rendering

E = html.escape


def fmt_when(s: str, now: dt.datetime, with_rel=True) -> str:
    t = dt.datetime.fromisoformat(s)
    days = (t.date() - now.date()).days
    base = t.strftime("%a %d %b, %H:%M").replace(", 00:00", "").replace(", 23:59", ", 23:59")
    if not with_rel:
        return base
    rel = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(
        days, f"in {days} days" if days > 0 else f"{-days} days ago")
    return f"{base} · {rel}"


def link(text: str, url: str) -> str:
    url = safe_url(url)
    return f'<a href="{E(url)}" target="_blank" rel="noopener">{E(text)}</a>' if url else E(text)


def section(title: str, count: int | None, body: str, empty: str, sid: str) -> str:
    badge = f'<span class="count">{count}</span>' if count else ""
    inner = body if body else f'<p class="empty">{E(empty)}</p>'
    return f'<section id="{sid}"><h2>{E(title)}{badge}</h2>{inner}</section>'


def safe_url(u: str | None) -> str:
    return u if isinstance(u, str) and u.startswith(("https://", "http://")) else ""


def render_brief(b: dict | None, now: dt.datetime) -> str:
    """Sections built from agent.py's LLM brief. Everything is escaped; links must be http(s)."""
    if not b:
        return ('<section class="errors"><h2>No agent brief yet</h2><p class="empty">'
                'Run <code>python3 agent.py</code> to build the schedule with join links.</p></section>')
    gen = dt.datetime.fromisoformat(b["_meta"]["generated_at"])
    stale = (f' <span class="tag bad">from {E(gen.strftime("%a %d %b"))}</span>'
             if gen.date() != now.date() else "")

    def t(s):
        try:
            return dt.datetime.fromisoformat(s).astimezone(TZ)
        except (TypeError, ValueError):
            return None

    # Sessions: today → +7 days, grouped by day; drop ones that ended >1h ago.
    horizon = now.date() + dt.timedelta(days=7)
    days: dict[dt.date, list] = {}
    for s in b.get("sessions", []):
        st, en = t(s.get("start")), t(s.get("end"))
        if not st or st.date() > horizon:
            continue
        if (en or st + dt.timedelta(hours=3)) < now - dt.timedelta(hours=1) and s.get("time_known", True):
            continue
        days.setdefault(st.date(), []).append((st, en, s))

    day_html = []
    for day in sorted(days):
        rel = {0: "Today", 1: "Tomorrow"}.get((day - now.date()).days, day.strftime("%A"))
        cards = []
        for st, en, s in sorted(days[day], key=lambda x: (x[0], x[2].get("course", ""))):
            when = (f'{st:%H:%M}' + (f'–{en:%H:%M}' if en else "")) if s.get("time_known", True) else "Time TBC"
            live = s.get("time_known", True) and st - dt.timedelta(minutes=15) <= now <= (en or st + dt.timedelta(hours=3))
            mode = s.get("mode") or "unknown"
            where = []
            if s.get("venue"):
                where.append(f'<span class="venue">📍 {E(s["venue"])}</span>')
            if (u := safe_url(s.get("join_url"))):
                plat = {"zoom": "Zoom", "teams": "Teams"}.get(s.get("platform"), "link")
                where.append(f'<a class="join" href="{E(u)}" target="_blank" rel="noopener">Join {plat}</a>')
            creds = " · ".join(x for x in [
                f'ID <code>{E(s["meeting_id"])}</code>' if s.get("meeting_id") else "",
                f'Passcode <code>{E(s["passcode"])}</code>' if s.get("passcode") else ""] if x)
            if creds:
                where.append(f'<span class="creds">{creds}</span>')
            if not where:
                where.append('<span class="tag bad">Location not found</span>')
            conf = s.get("confidence")
            conf_tag = f' <span class="tag {"warn" if conf == "low" else ""}">{E(conf)} confidence</span>' if conf in ("low", "medium") else ""
            extra = "".join(f'<p class="excerpt"><strong>{lbl}:</strong> {E(s[k])}</p>'
                            for k, lbl in (("prep", "Before class"), ("notes", "Note")) if s.get(k))
            src = safe_url(s.get("source_url"))
            src_html = f' · <a href="{E(src)}" target="_blank" rel="noopener">source</a>' if src else ""
            cards.append(
                f'<li class="session {"live" if live else ""}"><div class="time">{E(when)}'
                f'{"<span class=\"tag new\">Now</span>" if live else ""}</div><div class="main">'
                f'<div><strong>{E(s.get("course", ""))}</strong> {E(s.get("title", ""))}</div>'
                f'<div class="meta">{E(s.get("kind", ""))} · {E(mode.replace("_", " "))}'
                f'{" · " + E(s["instructor"]) if s.get("instructor") else ""}{src_html}{conf_tag}</div>'
                f'<div class="where">{"".join(where)}</div>{extra}</div></li>')
        day_html.append(f'<h3>{E(rel)} <span class="meta">{day:%d %b}</span></h3><ul class="list">{"".join(cards)}</ul>')
    sessions_html = "".join(day_html)

    prio = {"high": 0, "medium": 1, "low": 2}
    far = "9999"
    tasks = sorted(b.get("tasks", []), key=lambda x: (x.get("due") or far, prio.get(x.get("priority"), 3)))
    task_rows = []
    for x in tasks:
        due = t(x.get("due"))
        if due and due < now - dt.timedelta(hours=12):
            continue
        cls = "hot" if x.get("priority") == "high" or (due and due - now < dt.timedelta(hours=48)) else ""
        task_rows.append(
            f'<li class="{cls}"><div class="main">{link(x.get("title", ""), safe_url(x.get("url")))}'
            f'<div class="meta">{E(x.get("course", ""))}</div>'
            f'{f"<p class=\"excerpt\">{E(x["details"])}</p>" if x.get("details") else ""}</div>'
            f'<div class="side"><div>{E(fmt_when(due.isoformat(), now)) if due else "no fixed time"}</div></div></li>')
    tasks_html = f'<ul class="list">{"".join(task_rows)}</ul>' if task_rows else ""

    kd = sorted(b.get("key_dates", []), key=lambda x: x.get("date") or far)
    kd_rows = []
    for x in kd:
        d0 = t(x.get("date")) or (t(x["date"] + "T00:00:00+08:00") if len(x.get("date", "")) == 10 else None)
        if d0 and d0.date() < now.date():
            continue
        kd_rows.append(
            f'<li><div class="main">{link(x.get("title", ""), safe_url(x.get("url")))}'
            f'<div class="meta">{E(x.get("course", ""))}</div>'
            f'{f"<p class=\"excerpt\">{E(x["details"])}</p>" if x.get("details") else ""}</div>'
            f'<div class="side">{E(fmt_when(d0.isoformat(), now)) if d0 else E(x.get("date", ""))}</div></li>')
    kd_html = f'<ul class="list">{"".join(kd_rows)}</ul>' if kd_rows else ""

    gaps = "".join(f'<li><strong>{E(g.get("course", ""))}</strong>: {E(g.get("issue", ""))}</li>'
                   for g in b.get("gaps", []))
    gaps_html = f'<section class="gaps"><h2>Couldn’t confirm from Canvas</h2><ul>{gaps}</ul></section>' if gaps else ""

    n_today = len(days.get(now.date(), []))
    return (f'<section class="headline"><h2>Today{stale}</h2><p>{E(b.get("headline", ""))}</p></section>'
            + section(f"Where you need to be (next 7 days)", n_today, sessions_html, "No classes found.", "schedule")
            + section("What to do", len(task_rows), tasks_html, "Nothing to do.", "todo")
            + gaps_html
            + section("Key dates this semester", None, kd_html, "No key dates found.", "keydates"))


def render(d: dict, now: dt.datetime, brief: dict | None = None) -> str:
    def urgency(r):
        h = (dt.datetime.fromisoformat(r["when"]) - now).total_seconds() / 3600
        return "hot" if h < 48 else "warm" if h < 24 * 5 else ""

    pending = [r for r in d["due"] if not r["submitted"] and not r["done"]]
    rows = []
    for r in d["due"]:
        if r["submitted"] or r["done"]:
            status = '<span class="tag ok">Submitted</span>' if r["submitted"] else '<span class="tag ok">Done</span>'
            cls = "muted"
        else:
            status = '<span class="tag">To do</span>'
            cls = urgency(r)
        pts = f' · {r["points"]:g} pts' if isinstance(r["points"], (int, float)) else ""
        kind = (r["kind"] or "").replace("_", " ")
        rows.append(f'<li class="{cls}"><div class="main">{link(r["title"], r["url"])}'
                    f'<div class="meta">{E(r["course"])} · {E(kind)}{pts}</div></div>'
                    f'<div class="side"><div>{E(fmt_when(r["when"], now))}</div>{status}</div></li>')
    due_html = f'<ul class="list">{"".join(rows)}</ul>' if rows else ""

    over_html = "".join(
        f'<li class="hot"><div class="main">{link(r["title"], r["url"])}<div class="meta">{E(r["course"])}</div></div>'
        f'<div class="side"><div>was due {E(fmt_when(r["when"], now))}</div><span class="tag bad">Missing</span></div></li>'
        for r in d["overdue"])
    over_html = f'<ul class="list">{over_html}</ul>' if over_html else ""

    ev_html = "".join(
        f'<li><div class="main">{link(r["title"], r["url"])}<div class="meta">{E(r["course"])}</div></div>'
        f'<div class="side">{E(fmt_when(r["when"], now))}</div></li>' for r in d["events"])
    ev_html = f'<ul class="list">{ev_html}</ul>' if ev_html else ""

    ann_html = "".join(
        f'<li class="{"new" if r["is_new"] else ""}"><div class="main">'
        f'{"<span class=\"tag new\">New</span> " if r["is_new"] else ""}{link(r["title"], r["url"])}'
        f'<div class="meta">{E(r["course"])} · {E(fmt_when(r["when"], now))}</div>'
        f'<p class="excerpt">{E(r["excerpt"])}</p></div></li>' for r in d["announcements"])
    ann_html = f'<ul class="list">{ann_html}</ul>' if ann_html else ""

    inbox_html = "".join(
        f'<li><div class="main"><strong>{E(r["subject"])}</strong>'
        f'<div class="meta">{E(r["from"])} · {E(r["course"])} · {E(fmt_when(r["when"], now))}</div>'
        f'<p class="excerpt">{E(r["excerpt"])}</p></div></li>' for r in d["inbox"])
    inbox_html = f'<ul class="list">{inbox_html}</ul>' if inbox_html else ""

    def gcell(g):
        if g["score"] is None:
            return '<span class="muted">—</span>'
        delta = ""
        if g["delta"]:
            delta = f' <span class="tag {"ok" if g["delta"] > 0 else "bad"}">{g["delta"]:+g}</span>'
        letter = f' ({E(g["letter"])})' if g["letter"] else ""
        return f'{g["score"]:g}%{letter}{delta}'
    grades_html = "".join(
        f'<tr><td><strong>{E(g["course"])}</strong><div class="meta">{E(g["name"])}</div></td>'
        f'<td class="num">{gcell(g)}</td></tr>' for g in d["grades"])
    grades_html = f'<table class="grades">{grades_html}</table>' if grades_html else ""

    graded_html = "".join(
        f'<li class="{"new" if r["is_new"] else ""}"><div class="main">'
        f'{"<span class=\"tag new\">New</span> " if r["is_new"] else ""}{link(r["title"], r["url"])}'
        f'<div class="meta">{E(r["course"])} · graded {E(fmt_when(r["when"], now))}</div></div>'
        f'<div class="side">{E(str(r["grade"] if r["grade"] is not None else r["score"]))}'
        f'{" / " + format(r["points"], "g") if isinstance(r["points"], (int, float)) else ""}</div></li>'
        for r in d["graded"])
    graded_html = f'<ul class="list">{graded_html}</ul>' if graded_html else ""

    files_html = "".join(
        f'<li><div class="main">{"<span class=\"tag new\">New</span> " if r["is_new"] else ""}'
        f'{link(r["name"], r["url"])}<div class="meta">{E(r["course"])}</div></div>'
        f'<div class="side">{E(fmt_when(r["when"], now))}</div></li>' for r in d["files"])
    files_html = f'<ul class="list">{files_html}</ul>' if files_html else ""

    notice_html = "".join(
        f'<li><div class="main"><strong>{E(r["title"])}</strong>'
        f'<p class="excerpt">{E(r["excerpt"])}</p></div></li>' for r in d["notices"])
    notice_html = (f'<section id="notices"><h2>NUS notices</h2><ul class="list">{notice_html}</ul></section>'
                   if notice_html else "")

    new_anns = sum(r["is_new"] for r in d["announcements"])
    stats = [
        ("To do (14 days)", len(pending), "hot" if any(urgency(r) == "hot" for r in pending) else ""),
        ("Missing", len(d["overdue"]), "hot" if d["overdue"] else ""),
        ("New announcements", new_anns, ""),
        ("Unread messages", len(d["inbox"]), ""),
    ]
    stats_html = "".join(f'<div class="stat {c}"><div class="n">{n}</div><div class="l">{E(l)}</div></div>'
                         for l, n, c in stats)

    err_html = ""
    if d["errors"]:
        items = "".join(f"<li><code>{E(k)}</code>: {E(v)}</li>" for k, v in sorted(d["errors"].items()))
        err_html = f'<section class="errors"><h2>Couldn’t load</h2><ul>{items}</ul></section>'

    since = (f'Changes since {E(fmt_when(d["prev_generated_at"], now, False))}'
             if d["prev_generated_at"] else "First run, so “new” means the last 24 hours")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Canvas Daily Digest</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e5e5e2;--accent:#2f5bd3;
--hot:#c4352b;--hot-bg:#fdf0ef;--warm:#b26a00;--ok:#2e7d4f;--new:#2f5bd3;--new-bg:#edf2fe}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#141416;--card:#1d1d20;--fg:#ececee;
--muted:#9a9aa2;--line:#2c2c31;--accent:#8aa8ff;--hot:#ff8a80;--hot-bg:#2a1a1a;--warm:#f0b35a;--ok:#7fd1a0;
--new:#8aa8ff;--new-bg:#1b2236}}}}
:root[data-theme="dark"]{{--bg:#141416;--card:#1d1d20;--fg:#ececee;--muted:#9a9aa2;--line:#2c2c31;--accent:#8aa8ff;
--hot:#ff8a80;--hot-bg:#2a1a1a;--warm:#f0b35a;--ok:#7fd1a0;--new:#8aa8ff;--new-bg:#1b2236}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
main{{max-width:880px;margin:0 auto;padding:28px 16px 64px}}
header h1{{margin:0;font-size:26px;letter-spacing:-.01em}}
header p{{margin:4px 0 0;color:var(--muted)}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:22px 0 8px}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}}
.stat .n{{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}}
.stat .l{{color:var(--muted);font-size:13px}}
.stat.hot .n{{color:var(--hot)}}
section{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-top:16px}}
h2{{font-size:16px;margin:0 0 10px;display:flex;align-items:center;gap:8px}}
.count{{font-size:12px;background:var(--line);border-radius:999px;padding:1px 8px;color:var(--muted)}}
.list{{list-style:none;margin:0;padding:0}}
.list li{{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-top:1px solid var(--line)}}
.list li:first-child{{border-top:0}}
.list li.hot .side>div:first-child{{color:var(--hot);font-weight:600}}
.list li.warm .side>div:first-child{{color:var(--warm)}}
.list li.muted{{opacity:.6}}
.main{{min-width:0}} .side{{text-align:right;white-space:nowrap;font-size:13px;color:var(--muted)}}
.meta{{color:var(--muted);font-size:13px}}
.excerpt{{margin:4px 0 0;font-size:13.5px;color:var(--fg);opacity:.85}}
a{{color:var(--accent);text-decoration:none}} a:hover{{text-decoration:underline}}
.tag{{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:6px;background:var(--line);color:var(--fg);margin-top:3px}}
.tag.ok{{color:var(--ok)}} .tag.bad{{color:var(--hot);background:var(--hot-bg)}} .tag.new{{color:var(--new);background:var(--new-bg)}}
.empty{{color:var(--muted);margin:0}}
table.grades{{width:100%;border-collapse:collapse}}
table.grades td{{padding:8px 0;border-top:1px solid var(--line);vertical-align:top}}
table.grades tr:first-child td{{border-top:0}}
td.num{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
.errors{{border-color:var(--hot)}} .errors h2{{color:var(--hot)}}
section.headline p{{margin:0;font-size:15.5px}}
#schedule h3{{font-size:14px;margin:14px 0 2px;text-transform:uppercase;letter-spacing:.04em}}
#schedule h3:first-of-type{{margin-top:0}}
li.session{{justify-content:flex-start}}
li.session .time{{width:96px;flex:none;font-weight:600;font-variant-numeric:tabular-nums}}
li.session.live{{background:var(--new-bg);margin:0 -10px;padding:10px;border-radius:10px}}
.where{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px}}
a.join{{background:var(--accent);color:var(--bg);font-weight:600;padding:4px 12px;border-radius:8px;font-size:13px}}
a.join:hover{{text-decoration:none;opacity:.9}}
.creds,.venue{{font-size:13px}} code{{font-size:12.5px;background:var(--line);padding:1px 5px;border-radius:5px}}
.tag.warn{{color:var(--warm)}}
.gaps{{border-color:var(--warm)}} .gaps h2{{color:var(--warm)}} .gaps ul{{margin:0;padding-left:18px}}
footer{{color:var(--muted);font-size:12px;margin-top:24px;text-align:center}}
@media (max-width:640px){{li.session .time{{width:auto}}.stats{{grid-template-columns:repeat(2,1fr)}}.list li{{flex-direction:column;gap:4px}}.side{{text-align:left}}}}
</style></head><body><main>
<header><h1>Canvas Daily Digest</h1>
<p>{E(d["user"])} · {E(now.strftime("%A %d %B %Y"))} · generated {E(now.strftime("%H:%M"))} SGT</p>
<p>{since}</p></header>
<div class="stats">{stats_html}</div>
{render_brief(brief, now)}
{err_html}
{notice_html}
{section("Missing / overdue", len(d["overdue"]), over_html, "Nothing missing.", "missing")}
{section(f"Due in the next {LOOKAHEAD_DAYS} days", len(pending), due_html, "Nothing due.", "due")}
{section("Upcoming events", len(d["events"]), ev_html, "No calendar events.", "events")}
{section(f"Announcements (last {LOOKBACK_DAYS} days)", new_anns, ann_html, "No announcements.", "announcements")}
{section("Unread inbox", len(d["inbox"]), inbox_html, "Inbox clear.", "inbox")}
{section(f"Recently graded (last {LOOKBACK_DAYS} days)", len(d["graded"]), graded_html, "Nothing graded recently.", "graded")}
{section("Course grades", None, grades_html, "No courses.", "grades")}
{section(f"Updated files (last {LOOKBACK_DAYS} days)", len(d["files"]), files_html, "No new files (or files tab hidden).", "files")}
<footer>Top sections: Claude agent scan of syllabi, pages, announcements, files &amp; inbox (agent.py). Lower sections: Canvas REST API, deterministic. Times are Asia/Singapore.</footer>
</main></body></html>
"""


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--now", help="ISO timestamp to pin 'now' (for reproducible runs)")
    a = ap.parse_args()

    now = (dt.datetime.fromisoformat(a.now).astimezone(TZ) if a.now
           else dt.datetime.now(TZ).replace(microsecond=0))
    base, token = load_config()
    data_dir, archive = a.out / "data", a.out / "archive"
    data_dir.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)

    stamp = now.strftime("%Y-%m-%d")
    earlier = sorted(p for p in data_dir.glob("2*.json") if p.stem < stamp)
    prev = json.loads(earlier[-1].read_text()) if earlier else None

    raw = collect(Canvas(base, token), now)
    raw["base_html"] = base.removesuffix("/api/v1")
    digest = build(raw, now, prev)
    briefs = sorted(p for p in data_dir.glob("brief-*.json") if p.stem <= f"brief-{stamp}")
    brief = json.loads(briefs[-1].read_text()) if briefs else None
    page = render(digest, now, brief)

    (data_dir / f"{stamp}.json").write_text(json.dumps(digest, indent=1, sort_keys=True))
    (archive / f"{stamp}.html").write_text(page)
    tmp = a.out / "index.html.tmp"
    tmp.write_text(page)
    tmp.replace(a.out / "index.html")
    print(f"{now.isoformat()} wrote {a.out / 'index.html'} "
          f"(due={len(digest['due'])} missing={len(digest['overdue'])} "
          f"ann={len(digest['announcements'])} inbox={len(digest['inbox'])} errors={len(digest['errors'])})")


if __name__ == "__main__":
    main()
