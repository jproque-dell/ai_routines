#!/usr/bin/env python3
"""
Reads data.json (produced by fetch_gerrit.py) and generates dashboard.html,
a self-contained static dashboard (no external calls at view time).

Usage: python3 build.py
"""
import json
import re
import html as htmlmod
from datetime import datetime, timezone

# Dell engineers who own/submit patches in this dataset. Used to tell "our team
# commented" apart from "a community reviewer commented" when inferring whether
# an action is expected from our side. Extend this if new team members show up
# as owners/commenters in future data pulls.
TEAM_MEMBERS = [
    "Nilesh Thathagar", "Abhishek Gupta", "Prasant Padhi", "Siddharth Kumar",
    "Jean Pierre Roquesalane", "Yian Zong", "Cuiye Liu", "Cherry Liu",
    "Amit Zauber", "Pavithra Mahadev", "Tony Saad", "Bryan Neumann",
]
BOTS = {"zuul"}


def esc(s):
    return htmlmod.escape(str(s or ""), quote=True)


def fmt_date(iso_like):
    if not iso_like:
        return ""
    s = iso_like.split(".")[0]
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%b %d, %Y")
    except ValueError:
        return iso_like


def _normalize_name(name):
    return frozenset(re.sub(r"[^a-z\s]", "", (name or "").lower()).split())


_TEAM_TOKENS = [_normalize_name(n) for n in TEAM_MEMBERS]


def is_team_member(name):
    toks = _normalize_name(name)
    if not toks:
        return False
    for t in _TEAM_TOKENS:
        if toks == t or toks.issubset(t) or t.issubset(toks) or len(toks & t) >= 2:
            return True
    return False


def is_bot(name):
    n = (name or "").strip()
    if not n:
        return False
    if n.lower() in BOTS:
        return True
    # Vendor/third-party CI accounts: "ExaScaler CI", "OpenStack CI", etc.
    return "CI" in n.split()


# Gerrit auto-appends a "Patch Set N: <Label><value>" line to every message,
# even ones with no human-written text (a bare vote), and layers in other
# machine-generated boilerplate around real commentary (inline-comment-count
# markers, rebase/edit bookkeeping, stale-vote-copy notices). Strip all of
# that off to isolate whatever a human actually typed, if anything.
_PATCH_SET_PREFIX = re.compile(r"^(Uploaded patch set \d+:\s*|Patch Set \d+:\s*)", re.IGNORECASE)
_VOTE_TOKEN = re.compile(r"(Code-Review|Verified|Workflow|Code-Style-Review|Review-Priority)[+-]?\d*")
_INLINE_MARKER = re.compile(r"\(\d+ comments?\)")
# Rebase/reupload/vote-copy bookkeeping gets attributed to whichever human
# triggered it (not a bot account), so this has to be stripped by content
# regardless of author. Phrased as whole constructs, not fragments, so a
# stripped phrase doesn't leave a dangling "Patch Set N" behind.
_SYSTEM_BOILERPLATE = re.compile(
    r"(patch set \d+ was rebased( on behalf of <?gerrit_account_\d+>?)?\.?|was rebased\.?|"
    r"new patch set was added with (the )?same tree,? parent tree,? and commit message as patch set \d+\.?|"
    r"commit message was updated\.?|published edit on patch set \d+\.?|"
    r"uploaded patch set \d+\.?|topic set to \S+\.?|"
    r"on behalf of <?gerrit_account_\d+>?|"
    r"removed\s*by <?gerrit_account_\d+>?|"
    r"outdated votes:.*|copied votes:.*|removed the following votes.*|"
    r"change has been successfully (rebased|cherry-picked|submitted)\.?|"
    r"this change was submitted with unreviewed changes.*)",
    re.IGNORECASE | re.DOTALL,
)

# Third-party CI systems post under all kinds of account names (some human-
# looking, e.g. a vendor engineer's personal account used to run their CI) so
# name-based bot detection alone misses them. Their messages are still
# recognizable as automated build reports by content, regardless of author.
_CI_REPORT_PATTERN = re.compile(
    r"\b(build (succeeded|failed|aborted)|failure in|success in|log path\s*:)", re.IGNORECASE)

# Humans on this team routinely trigger vendor third-party CI by leaving a
# specific command phrase as a (patchset-level) comment — "run-DellEMC
# PowerScale CI", plus the standard Zuul "recheck"/"reverify" commands. These
# are real text a person typed, but they're operating a CI system, not
# reviewing — filter them the same as automated CI output.
_CI_TRIGGER_PATTERN = re.compile(
    r"^(recheck|reverify|check experimental|run[-\s][\w\s-]*ci\b[\w\s-]*)$", re.IGNORECASE)


def is_ci_noise(text):
    t = (text or "").strip()
    return bool(_CI_REPORT_PATTERN.search(t) or _CI_TRIGGER_PATTERN.match(t))


def classify_message(c):
    """Sort one top-level Gerrit message into 'human' (has real written text)
    or 'noise' (bookkeeping/vote/CI-report/CI-trigger/bare inline-comment-count
    marker with nothing a person actually wrote in the message itself). The
    bare "(N comments)" marker used to be treated as a soft substantive
    signal, but the real inline-comment endpoint (fetched separately)
    supersedes it with the actual text and per-thread resolved state, so it's
    just noise here now — keeping it as a fallback risked citing a thread
    that's since been resolved.
    """
    raw = c.get("text") or ""
    if is_ci_noise(raw):
        return "noise", ""
    t = _PATCH_SET_PREFIX.sub("", raw.strip())
    t = _VOTE_TOKEN.sub("", t)
    t = _INLINE_MARKER.sub("", t)
    t = _SYSTEM_BOILERPLATE.sub("", t)
    t = t.strip(" \n\t,.")
    if is_ci_noise(t):
        return "noise", ""
    if t:
        return "human", t
    return "noise", ""


def is_real_comment(c):
    kind, _ = classify_message(c)
    return kind == "human"


def last_substantive_message(comments):
    """Most recent top-level message that isn't just bookkeeping/a vote/a bot post."""
    for c in reversed(comments or []):
        if is_bot(c.get("author")):
            continue
        if not is_real_comment(c):
            continue
        return c
    return None


def latest_per_inline_thread(inline_comments):
    """Gerrit anchors each inline discussion to a (file, line) span; the most
    recent comment in that span carries the thread's current resolved state.
    Approximates threads by (file, line) — good enough since Gerrit rarely
    reuses one line for two unrelated conversations.
    """
    latest = {}
    for c in inline_comments or []:
        if is_bot(c.get("author")) or is_ci_noise(c.get("text")):
            continue
        key = (c.get("file"), c.get("line"))
        prev = latest.get(key)
        if prev is None or (c.get("date") or "") >= (prev.get("date") or ""):
            latest[key] = c
    return latest


def open_reviewer_threads(inline_comments):
    """Inline threads still marked unresolved whose last word belongs to a
    reviewer, not our team — i.e. the ball is genuinely in our court.
    """
    threads = [c for c in latest_per_inline_thread(inline_comments).values()
               if c.get("unresolved") and not is_team_member(c.get("author"))]
    threads.sort(key=lambda c: c.get("date") or "", reverse=True)
    return threads


def compute_action(lv):
    """Infer whether a reviewer is waiting on an action from our side.

    Priority: merged/abandoned patches need nothing further; a merge conflict
    against the target branch (Gerrit's own mergeability check — the same
    signal behind the "Merge Conflict" box on the change page) blocks
    everything else and always needs a rebase from us first; a live -1/-2
    from a non-team Code-Review voter means changes were explicitly
    requested; Gerrit's own "unresolved" flag on an inline comment thread
    whose last word is a reviewer's is the most authoritative "reply
    expected" signal; failing that, a substantive top-level message left
    unanswered by our side falls back to the same verdict; no review
    engagement yet just means it's waiting in the queue, not on us; anything
    else with positive engagement is on track.
    """
    if not lv.get("found"):
        return {"level": "unknown", "label": "Unresolved", "detail": lv.get("error", "")}

    status = lv.get("status")
    if status in ("MERGED", "ABANDONED"):
        return {"level": "none", "label": "No action needed",
                "detail": "Merged" if status == "MERGED" else "Abandoned"}

    if lv.get("mergeable") is False:
        return {"level": "bad", "label": "Merge conflict",
                "detail": "Gerrit reports this patch conflicts with its target branch — needs a rebase before it can be reviewed or merged."}

    cr_votes = ((lv.get("code_review") or {}).get("votes")) or []
    negative = [(n, v) for n, v in cr_votes if v < 0 and not is_team_member(n)]
    if negative:
        name, val = negative[0]
        return {"level": "bad", "label": "Changes requested",
                "detail": f"{name} left {val:+d} on Code-Review"}

    open_threads = open_reviewer_threads(lv.get("inline_comments"))
    if open_threads:
        top = open_threads[0]
        where = f" ({top['file'].lstrip('/')}:{top['line']})" if top.get("file") and top.get("file") != "/PATCHSET_LEVEL" and top.get("line") else ""
        more = f" (+{len(open_threads) - 1} more open thread{'s' if len(open_threads) != 2 else ''})" if len(open_threads) > 1 else ""
        return {"level": "warn", "label": "Reviewer comment — reply expected",
                "detail": f"{top['author']}{where}: “{top['text']}”{more}"}

    last = last_substantive_message(lv.get("comments"))
    if last and not is_team_member(last["author"]):
        _, clean_text = classify_message(last)
        return {"level": "warn", "label": "Reviewer comment — reply expected",
                "detail": f"{last['author']}: “{clean_text}”"}

    had_inline_engagement = bool(latest_per_inline_thread(lv.get("inline_comments")))
    if not cr_votes and not had_inline_engagement and last is None:
        return {"level": "neutral", "label": "Awaiting reviewer attention",
                "detail": "No reviewer has weighed in yet"}

    if not cr_votes:
        return {"level": "good", "label": "On track",
                "detail": "Reviewed via inline comments, all resolved — no formal Code-Review vote cast yet"}

    return {"level": "good", "label": "On track", "detail": "No pending ask from reviewers"}


def main():
    with open("data.json") as f:
        data = json.load(f)

    rows = data["rows"]
    live = data["live"]
    fetched_at = data["fetched_at"]
    fetched_dt = datetime.fromisoformat(fetched_at)
    fetched_label = fetched_dt.strftime("%B %d, %Y at %H:%M UTC")

    # enrich rows with live info + inferred action-needed verdict
    for r in rows:
        r["_live"] = live.get(r["patch_id"], {"found": False, "error": "no id"})
        r["_action"] = compute_action(r["_live"])

    total = len(rows)
    merged = sum(1 for r in rows if r["_live"].get("status") == "MERGED")
    open_ = sum(1 for r in rows if r["_live"].get("status") == "NEW")
    abandoned = sum(1 for r in rows if r["_live"].get("status") == "ABANDONED")
    unresolved = sum(1 for r in rows if not r["_live"].get("found"))
    action_needed = sum(1 for r in rows if r["_action"]["level"] in ("bad", "warn"))
    merge_conflicts = sum(1 for r in rows if r["_live"].get("mergeable") is False)

    ready_to_merge = 0
    for r in rows:
        lv = r["_live"]
        if lv.get("status") != "NEW" or lv.get("mergeable") is False:
            continue
        cr = (lv.get("code_review") or {}).get("state", "")
        ver = (lv.get("verified") or {}).get("state", "")
        if cr == "approved" and ver == "approved":
            ready_to_merge += 1

    platforms = sorted({r["platform"] for r in rows if r["platform"]})
    components = sorted({r["component"] for r in rows if r["component"]})

    ACTION_PILL_CLASS = {
        "bad": "pill-bad", "warn": "pill-warn", "good": "pill-good",
        "none": "pill-unknown", "neutral": "pill-unknown", "unknown": "pill-unknown",
    }

    def action_pill(action):
        cls = ACTION_PILL_CLASS.get(action["level"], "pill-unknown")
        title = esc(action["detail"])
        return f'<span class="pill {cls}" title="{title}">{esc(action["label"])}</span>'

    def status_pill(lv):
        if not lv.get("found"):
            return '<span class="pill pill-unknown">Unresolved</span>'
        st = lv.get("status", "UNKNOWN")
        cls = {"MERGED": "pill-good", "NEW": "pill-warn", "ABANDONED": "pill-bad"}.get(st, "pill-unknown")
        label = {"MERGED": "Merged", "NEW": "Open", "ABANDONED": "Abandoned"}.get(st, st.title())
        pill = f'<span class="pill {cls}">{label}</span>'
        if lv.get("mergeable") is False:
            pill += (' <span class="pill pill-bad" title="Gerrit reports a merge conflict against the '
                     'target branch — the same signal behind the \'Merge Conflict\' box on the change page">'
                     '⚠ Conflict</span>')
        return pill

    def vote_badge(label_data, name):
        if not label_data:
            return f'<span class="vote vote-none">{name} —</span>'
        state = label_data.get("state", "none")
        by = label_data.get("by", "")
        if state == "approved":
            return f'<span class="vote vote-good" title="{esc(by)}">{name} ✓</span>'
        if state == "rejected":
            return f'<span class="vote vote-bad" title="{esc(by)}">{name} ✕</span>'
        if state == "none":
            return f'<span class="vote vote-none">{name} —</span>'
        val = state
        cls = "vote-good" if val.startswith("+") else "vote-warn"
        return f'<span class="vote {cls}" title="{esc(by)}">{name} {esc(val)}</span>'

    def short_file(path):
        if not path or path == "/PATCHSET_LEVEL":
            return ""
        parts = path.lstrip("/").split("/")
        return "/".join(parts[-2:]) if len(parts) > 1 else parts[0]

    def build_row(r):
        lv = r["_live"]
        action = r["_action"]
        pid = r["patch_id"]
        url = lv.get("url") or f"https://review.opendev.org/q/{pid}"
        subject = lv.get("subject") or r["description"]
        owner_live = lv.get("owner") or r["owner"]
        raw_comments = lv.get("comments") or []
        inline_comments = lv.get("inline_comments") or []
        open_threads = open_reviewer_threads(inline_comments)
        verdict_item = open_threads[0] if open_threads else last_substantive_message(raw_comments)

        # Real written commentary from two Gerrit sources: top-level messages
        # with actual prose (not bookkeeping/votes/CI), and inline (per-line)
        # review comments, which is where most real back-and-forth actually
        # happens and the /detail endpoint alone never surfaces.
        top_written = []
        top_noise = 0
        for c in raw_comments:
            if is_bot(c["author"]):
                top_noise += 1
                continue
            kind, text = classify_message(c)
            if kind != "human":
                top_noise += 1
                continue
            top_written.append({"author": c["author"], "date": c["date"], "text": text,
                                 "file": None, "line": None, "unresolved": False, "_src": c})

        inline_written = []
        inline_noise = 0
        for c in inline_comments:
            if is_bot(c["author"]) or is_ci_noise(c["text"]):
                inline_noise += 1
                continue
            inline_written.append({"author": c["author"], "date": c["date"], "text": c["text"],
                                    "file": c.get("file"), "line": c.get("line"),
                                    "unresolved": c.get("unresolved", False), "_src": c})

        combined = sorted(top_written + inline_written, key=lambda x: x["date"] or "", reverse=True)
        hidden_noise = top_noise + inline_noise

        SHOWN = 6
        shown = combined[:SHOWN]
        hidden_older_written = len(combined) - len(shown)

        # Guarantee the comment driving the Action verdict is visible even if
        # it fell outside the most-recent-N window (an old unresolved thread).
        verdict_in_shown = verdict_item is not None and any(
            item["_src"] is verdict_item for item in shown)
        if verdict_item is not None and not verdict_in_shown:
            is_inline_verdict = "file" in verdict_item
            verdict_text = verdict_item.get("text", "") if is_inline_verdict else classify_message(verdict_item)[1]
            v = {"author": verdict_item["author"], "date": verdict_item["date"],
                 "text": verdict_text, "file": verdict_item.get("file"),
                 "line": verdict_item.get("line"), "unresolved": verdict_item.get("unresolved", False),
                 "_src": verdict_item}
            shown = [v] + shown[:SHOWN - 1]
            hidden_older_written = max(0, hidden_older_written - 1)

        comment_html = ""
        if shown:
            item_parts = []
            for item in shown:
                is_verdict = verdict_item is not None and item["_src"] is verdict_item
                cls = "msg-verdict" if is_verdict else ""
                flag = (
                    ' <span class="msg-flag" title="This is the specific comment the Action column\'s verdict '
                    'for this row is based on — its content is what \'Changes requested\' / \'Reply expected\' etc. '
                    'above is quoting.">↳ drives verdict</span>'
                ) if is_verdict else ""
                loc = short_file(item["file"])
                loc_html = f' <span class="c-loc">{esc(loc)}{":" + str(item["line"]) if item["line"] else ""}</span>' if loc else ""
                open_flag = (
                    ' <span class="c-open" title="Gerrit still marks this specific comment thread unresolved, '
                    'and the last word in it belongs to a reviewer, not the team — it hasn\'t been replied to '
                    'and marked resolved yet.">open thread</span>'
                ) if item["unresolved"] and not is_team_member(item["author"]) else ""
                item_parts.append(
                    f'<li class="{cls}"><span class="c-author">{esc(item["author"])}</span> '
                    f'<span class="c-date">{esc(fmt_date(item["date"]))}</span>{loc_html}{open_flag}{flag}'
                    f'<div class="c-text">{esc(item["text"])}</div></li>'
                )
            hidden_notes = []
            if hidden_older_written:
                hidden_notes.append(f"{hidden_older_written} earlier comment{'s' if hidden_older_written != 1 else ''} not shown")
            if hidden_noise:
                hidden_notes.append(f"{hidden_noise} CI/vote/system message{'s' if hidden_noise != 1 else ''} hidden")
            hidden_note_html = f'<p class="hidden-note">{esc(" · ".join(hidden_notes))}</p>' if hidden_notes else ""
            comment_html = f'<ul class="comments">{"".join(item_parts)}</ul>{hidden_note_html}'
        elif hidden_noise:
            comment_html = f'<p class="no-comments">No written comments yet ({hidden_noise} CI/vote/system message{"s" if hidden_noise != 1 else ""} hidden).</p>'
        else:
            comment_html = '<p class="no-comments">No review messages found.</p>'
        comment_count = len(combined)

        updated = fmt_date(lv.get("updated", ""))
        note = f'<p class="row-note">{esc(r["note"])}</p>' if r.get("note") else ""

        search_blob = " ".join([
            pid, r["platform"], r["component"], r["type"], r["description"],
            r["owner"], subject, owner_live, action["label"], action["detail"],
        ]).lower()

        return f"""
        <tr class="patch-row" data-platform="{esc(r['platform'])}" data-component="{esc(r['component'])}"
            data-status="{esc(lv.get('status', 'UNKNOWN'))}" data-action="{esc(action['level'])}"
            data-search="{esc(search_blob)}">
          <td class="col-id">
            <a href="{esc(url)}" target="_blank" rel="noopener" class="patch-link">#{esc(pid)}</a>
            <span class="row-type">{esc(r['type'])}</span>
          </td>
          <td class="col-desc">
            <div class="desc-main">{esc(r['description'])}</div>
            <div class="desc-meta">{esc(r['platform'])} · {esc(r['component'])} · {esc(owner_live)}</div>
            {note}
          </td>
          <td class="col-status">{status_pill(lv)}</td>
          <td class="col-action">{action_pill(action)}</td>
          <td class="col-votes">
            {vote_badge(lv.get('verified'), 'CI')}
            {vote_badge(lv.get('code_review'), 'CR')}
          </td>
          <td class="col-updated">{esc(updated)}</td>
          <td class="col-comments">
            <details>
              <summary>{comment_count} comment{'s' if comment_count != 1 else ''}</summary>
              {comment_html}
            </details>
          </td>
        </tr>"""

    rows_html = "\n".join(build_row(r) for r in rows)

    platform_options = "".join(f'<option value="{esc(p)}">{esc(p)}</option>' for p in platforms)
    component_options = "".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in components)

    html_out = f"""<title>Hibiscus Patch Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #f5f7fa;
    --surface: #ffffff;
    --surface-2: #eef1f5;
    --border: #dde2e9;
    --text: #1b2027;
    --text-muted: #5b6472;
    --text-faint: #8891a0;
    --accent: #2f6690;
    --accent-soft: #e7eff5;
    --good: #1f8a5f;
    --good-soft: #e3f5ec;
    --warn: #a8710b;
    --warn-soft: #fbf0dd;
    --bad: #b3261e;
    --bad-soft: #fbe8e6;
    --neutral: #6b7280;
    --neutral-soft: #eceef1;
    --shadow: 0 1px 2px rgba(20, 24, 31, 0.06), 0 1px 1px rgba(20, 24, 31, 0.04);
    --radius: 10px;
    --font-display: 'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', sans-serif;
    --font-mono: 'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Consolas, monospace;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #11151b;
      --surface: #171c24;
      --surface-2: #1e2530;
      --border: #2a3140;
      --text: #e7ebf1;
      --text-muted: #9aa4b5;
      --text-faint: #6b7484;
      --accent: #7cabd4;
      --accent-soft: #1d2c3a;
      --good: #3fc98e;
      --good-soft: #16302a;
      --warn: #e0b23a;
      --warn-soft: #332a13;
      --bad: #ff8078;
      --bad-soft: #3a1f1e;
      --neutral: #8b93a1;
      --neutral-soft: #232a35;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #11151b;
    --surface: #171c24;
    --surface-2: #1e2530;
    --border: #2a3140;
    --text: #e7ebf1;
    --text-muted: #9aa4b5;
    --text-faint: #6b7484;
    --accent: #7cabd4;
    --accent-soft: #1d2c3a;
    --good: #3fc98e;
    --good-soft: #16302a;
    --warn: #e0b23a;
    --warn-soft: #332a13;
    --bad: #ff8078;
    --bad-soft: #3a1f1e;
    --neutral: #8b93a1;
    --neutral-soft: #232a35;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
  }}

  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-display);
    margin: 0;
    padding: 0 0 4rem;
    -webkit-font-smoothing: antialiased;
  }}
  a {{ color: var(--accent); }}
  a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible {{
    outline: 2px solid var(--accent); outline-offset: 2px;
  }}

  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 2.5rem 1.5rem 0; }}

  header.top {{
    display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 0.75rem;
    margin-bottom: 0.35rem;
  }}
  .eyebrow {{
    font-family: var(--font-mono); font-size: 0.72rem; letter-spacing: 0.09em; text-transform: uppercase;
    color: var(--accent); font-weight: 600;
  }}
  h1 {{ font-size: 1.9rem; font-weight: 700; margin: 0.15rem 0 0.3rem; text-wrap: balance; }}
  .subtitle {{ color: var(--text-muted); font-size: 0.95rem; max-width: 62ch; line-height: 1.5; margin: 0 0 1.6rem; }}
  .meta-note {{
    font-size: 0.8rem; color: var(--text-faint); font-family: var(--font-mono);
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 0.55rem 0.8rem; display: inline-block; margin-bottom: 2rem; line-height: 1.5;
  }}
  .meta-note b {{ color: var(--text-muted); font-weight: 600; }}

  .stats {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 0.75rem; margin-bottom: 2rem;
  }}
  .stat {{
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 0.9rem 1rem; box-shadow: var(--shadow);
  }}
  .stat .n {{ font-family: var(--font-mono); font-size: 1.6rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .stat .l {{ font-size: 0.76rem; color: var(--text-muted); margin-top: 0.2rem; }}
  .stat.good .n {{ color: var(--good); }}
  .stat.warn .n {{ color: var(--warn); }}
  .stat.bad .n {{ color: var(--bad); }}
  .stat.accent .n {{ color: var(--accent); }}

  .controls {{
    display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; margin-bottom: 1.1rem;
  }}
  .controls input[type="search"] {{
    flex: 1 1 220px; padding: 0.55rem 0.8rem; border-radius: 8px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text); font-family: var(--font-display); font-size: 0.9rem;
  }}
  .controls select {{
    padding: 0.55rem 0.7rem; border-radius: 8px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text); font-family: var(--font-display); font-size: 0.85rem;
  }}
  .count-note {{ font-size: 0.8rem; color: var(--text-faint); margin: -0.4rem 0 1rem; }}

  .table-scroll {{
    overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius);
    background: var(--surface); box-shadow: var(--shadow);
  }}
  table {{ border-collapse: collapse; width: 100%; min-width: 860px; }}
  thead th {{
    text-align: left; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--text-faint); font-weight: 600; padding: 0.7rem 0.9rem;
    border-bottom: 1px solid var(--border); background: var(--surface-2);
    position: sticky; top: 0;
  }}
  tbody tr {{ border-bottom: 1px solid var(--border); }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: var(--surface-2); }}
  td {{ padding: 0.8rem 0.9rem; vertical-align: top; font-size: 0.87rem; }}

  .col-id {{ white-space: nowrap; }}
  .patch-link {{ font-family: var(--font-mono); font-weight: 600; text-decoration: none; }}
  .patch-link:hover {{ text-decoration: underline; }}
  .row-type {{ display: block; font-size: 0.7rem; color: var(--text-faint); margin-top: 0.25rem; }}

  .desc-main {{ font-weight: 500; line-height: 1.4; }}
  .desc-meta {{ font-size: 0.76rem; color: var(--text-muted); margin-top: 0.25rem; }}
  .row-note {{ font-size: 0.76rem; color: var(--text-faint); margin: 0.35rem 0 0; font-style: italic; }}

  .col-updated {{ white-space: nowrap; font-family: var(--font-mono); font-size: 0.8rem; color: var(--text-muted); }}

  .pill {{
    display: inline-block; padding: 0.2rem 0.55rem; border-radius: 999px; font-size: 0.74rem; font-weight: 600;
  }}
  .pill-good {{ background: var(--good-soft); color: var(--good); }}
  .pill-warn {{ background: var(--warn-soft); color: var(--warn); }}
  .pill-bad {{ background: var(--bad-soft); color: var(--bad); }}
  .pill-unknown {{ background: var(--neutral-soft); color: var(--neutral); }}

  .col-votes {{ white-space: nowrap; }}
  .vote {{
    display: inline-block; font-family: var(--font-mono); font-size: 0.74rem; font-weight: 600;
    padding: 0.15rem 0.4rem; border-radius: 5px; margin-right: 0.3rem; cursor: default;
  }}
  .vote-good {{ background: var(--good-soft); color: var(--good); }}
  .vote-warn {{ background: var(--warn-soft); color: var(--warn); }}
  .vote-bad {{ background: var(--bad-soft); color: var(--bad); }}
  .vote-none {{ background: var(--neutral-soft); color: var(--text-faint); }}

  .col-comments {{ min-width: 200px; }}
  details summary {{
    cursor: pointer; font-size: 0.78rem; color: var(--accent); font-weight: 600; list-style: none;
  }}
  details summary::-webkit-details-marker {{ display: none; }}
  details summary::before {{ content: '▸ '; }}
  details[open] summary::before {{ content: '▾ '; }}
  ul.comments {{ list-style: none; margin: 0.5rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }}
  ul.comments li {{
    background: var(--surface-2); border-radius: 6px; padding: 0.45rem 0.6rem; font-size: 0.78rem;
  }}
  .c-author {{ font-weight: 600; }}
  .c-date {{ color: var(--text-faint); font-family: var(--font-mono); font-size: 0.7rem; margin-left: 0.3rem; }}
  .c-loc {{
    font-family: var(--font-mono); font-size: 0.68rem; color: var(--accent); background: var(--accent-soft);
    padding: 0.05rem 0.35rem; border-radius: 4px; margin-left: 0.3rem;
  }}
  .c-open {{
    font-size: 0.65rem; font-weight: 600; color: var(--bad); text-transform: uppercase; letter-spacing: 0.03em;
    margin-left: 0.3rem;
  }}
  .c-text {{ margin-top: 0.2rem; color: var(--text-muted); line-height: 1.4; }}
  .no-comments {{ font-size: 0.78rem; color: var(--text-faint); margin: 0.4rem 0 0; }}
  ul.comments li.msg-verdict {{ background: var(--warn-soft); box-shadow: inset 0 0 0 1px var(--warn); }}
  ul.comments li.msg-verdict .c-text {{ color: var(--text); }}
  .msg-flag {{
    font-size: 0.68rem; font-weight: 600; color: var(--warn); text-transform: uppercase; letter-spacing: 0.04em;
  }}
  .hidden-note {{ font-size: 0.7rem; color: var(--text-faint); margin: 0.5rem 0 0; font-style: italic; }}

  .empty-state {{ text-align: center; padding: 2.5rem 1rem; color: var(--text-faint); font-size: 0.9rem; }}

  footer.explainer {{
    max-width: 1180px; margin: 2.5rem auto 0; padding: 0 1.5rem;
    font-size: 0.78rem; color: var(--text-faint); line-height: 1.6;
  }}
  footer.explainer code {{
    font-family: var(--font-mono); background: var(--surface-2); padding: 0.1rem 0.35rem; border-radius: 4px;
    color: var(--text-muted);
  }}

  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style>

<div class="wrap">
  <header class="top">
    <div>
      <div class="eyebrow">Dell Storage Drivers · OpenStack</div>
      <h1>2026.2 Hibiscus Patch Board</h1>
    </div>
  </header>
  <p class="subtitle">Live status, CI results, and review votes for every Gerrit change tracked for the Hibiscus release cycle, pulled directly from opendev.org.</p>
  <div class="meta-note"><b>Data as of</b> {esc(fetched_label)} — snapshot fetched server-side from review.opendev.org.</div>

  <div class="stats">
    <div class="stat accent"><div class="n">{total}</div><div class="l">Total patches</div></div>
    <div class="stat good"><div class="n">{merged}</div><div class="l">Merged</div></div>
    <div class="stat warn"><div class="n">{open_}</div><div class="l">Open in Gerrit</div></div>
    <div class="stat bad"><div class="n">{action_needed}</div><div class="l">Action needed<br>from us</div></div>
    <div class="stat bad"><div class="n">{merge_conflicts}</div><div class="l">Merge conflicts</div></div>
    <div class="stat good"><div class="n">{ready_to_merge}</div><div class="l">Ready to merge<br>(CI+CR approved)</div></div>
    <div class="stat bad"><div class="n">{abandoned}</div><div class="l">Abandoned</div></div>
  </div>

  <div class="controls">
    <input type="search" id="search" placeholder="Search patch id, description, owner…" aria-label="Search patches">
    <select id="filter-platform" aria-label="Filter by storage platform">
      <option value="">All platforms</option>
      {platform_options}
    </select>
    <select id="filter-status" aria-label="Filter by status">
      <option value="">All statuses</option>
      <option value="MERGED">Merged</option>
      <option value="NEW">Open</option>
      <option value="ABANDONED">Abandoned</option>
    </select>
    <select id="filter-action" aria-label="Filter by action needed">
      <option value="">All actions</option>
      <option value="bad">Changes requested / merge conflict</option>
      <option value="warn">Reviewer reply expected</option>
      <option value="neutral">Awaiting reviewer attention</option>
      <option value="good">On track</option>
      <option value="none">No action needed (closed)</option>
    </select>
  </div>
  <p class="count-note" id="count-note"></p>

  <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Patch</th>
          <th>Description</th>
          <th>Status</th>
          <th>Action needed from us</th>
          <th>Votes</th>
          <th>Updated</th>
          <th>Review activity</th>
        </tr>
      </thead>
      <tbody id="patch-body">
        {rows_html}
      </tbody>
    </table>
  </div>
  <div class="empty-state" id="empty-state" style="display:none;">No patches match your filters.</div>
</div>

<footer class="explainer">
  opendev.org's Gerrit REST API doesn't send CORS headers, and this page's sandbox blocks requests to any host but Google Fonts — so this table can't poll live from your browser. It's a snapshot fetched server-side and baked into the page.
  To refresh it: re-run <code>fetch_gerrit.py --section "2026.2 Hibiscus"</code> then <code>build.py</code> in the dashboard working directory, and republish.
  <br><br>
  <b>"Action needed from us"</b> is inferred, not authoritative: <b>Merge conflict</b> means Gerrit's own mergeability check — the same one behind the "Merge Conflict" box on the change page — reports this patch can't be merged as-is against its target branch; <b>Changes requested</b> means a non-team reviewer currently has a &minus;1/&minus;2 Code-Review vote standing; <b>Reviewer reply expected</b> means Gerrit still marks an inline comment thread (or, failing that, the last written message) as coming from a reviewer and unanswered (hover the pill for it); <b>Awaiting reviewer attention</b> means no one has reviewed it yet; <b>On track</b> means there's reviewer engagement with nothing outstanding — including patches reviewed entirely through inline comments that were all resolved without a formal vote. Always open the change to confirm before acting.
  <br><br>
  In the <b>Review activity</b> column, an <span class="c-open">open thread</span> tag on a comment means Gerrit still has that specific inline discussion marked unresolved and the last word in it belongs to a reviewer — nobody on the team has replied and marked it resolved. That's the same signal driving the Action column; the tag just points to which comment below is the open one.
  A comment highlighted with <span class="msg-flag">↳ drives verdict</span> is the exact one the Action column's verdict for that row was computed from — when several comments are shown, this is the one being quoted above, not just the most recent.
</footer>

<script>
(function() {{
  const search = document.getElementById('search');
  const platformSel = document.getElementById('filter-platform');
  const statusSel = document.getElementById('filter-status');
  const actionSel = document.getElementById('filter-action');
  const rows = Array.from(document.querySelectorAll('.patch-row'));
  const emptyState = document.getElementById('empty-state');
  const countNote = document.getElementById('count-note');

  function applyFilters() {{
    const q = search.value.trim().toLowerCase();
    const platform = platformSel.value;
    const status = statusSel.value;
    const action = actionSel.value;
    let visible = 0;
    rows.forEach(function(row) {{
      const matchesQ = !q || row.dataset.search.includes(q);
      const matchesPlatform = !platform || row.dataset.platform === platform;
      const matchesStatus = !status || row.dataset.status === status;
      const matchesAction = !action || row.dataset.action === action;
      const show = matchesQ && matchesPlatform && matchesStatus && matchesAction;
      row.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    emptyState.style.display = visible === 0 ? '' : 'none';
    countNote.textContent = visible + ' of ' + rows.length + ' patches shown';
  }}

  search.addEventListener('input', applyFilters);
  platformSel.addEventListener('change', applyFilters);
  statusSel.addEventListener('change', applyFilters);
  actionSel.addEventListener('change', applyFilters);
  applyFilters();
}})();
</script>
"""

    with open("dashboard.html", "w") as f:
        f.write(html_out)
    print("Wrote dashboard.html")


if __name__ == "__main__":
    main()
