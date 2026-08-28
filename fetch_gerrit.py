#!/usr/bin/env python3
"""
Fetch live status/review data from opendev.org's public Gerrit REST API for the
patches listed in dataset.py, and write the merged result to data.json.

Usage: python3 fetch_gerrit.py [--section "2026.2 Hibiscus"]
  --section limits which release section(s) get fetched (matches ROWS[i]['section']
  exactly). Omit to fetch every Gerrit-backed row in the dataset.

Re-run this any time to refresh data.json, then re-run build.py to regenerate
dashboard.html.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dataset import ROWS, OSS_TICKETS, RED_HAT, SECTION_ORDER

GERRIT_BASE = "https://review.opendev.org"
XSSI_PREFIX = ")]}'"


def fetch_inline_comments(patch_id):
    """Real per-line (and patchset-level) review comments — the actual text of
    what a reviewer wrote, which the /detail messages endpoint doesn't carry
    (it only auto-notes "(N comments)" without the content). This is where
    genuine back-and-forth discussion actually lives on most Gerrit changes.
    """
    url = f"{GERRIT_BASE}/changes/{patch_id}/comments"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:  # noqa: BLE001
        return []

    if raw.startswith(XSSI_PREFIX):
        raw = raw[len(XSSI_PREFIX):]
    try:
        by_file = json.loads(raw)
    except json.JSONDecodeError:
        return []

    out = []
    for file_path, entries in (by_file or {}).items():
        for c in entries:
            author = (c.get("author") or {}).get("name", "unknown")
            message = (c.get("message") or "").strip()
            if not message:
                continue
            out.append({
                "author": author,
                "text": message,
                "date": c.get("updated", ""),
                "file": file_path,
                "line": c.get("line") or (c.get("range") or {}).get("start_line"),
                "unresolved": bool(c.get("unresolved")),
                "patch_set": c.get("patch_set"),
            })
    return out


def fetch_conflicts(patch_id):
    """The "Merge conflicts" box on a Gerrit change page, reproduced exactly:
    Gerrit's `conflicts:<id>` search operator, which lists every other *open*
    change on the branch whose diff overlaps this one's closely enough that
    merging one would break a clean merge of the other. This is a distinct,
    pairwise check against other in-flight changes — NOT the same thing as
    the /revisions/{rev}/mergeable endpoint, which only tests whether this
    patch set applies cleanly onto the *current* tip of its target branch.
    A patch can pass that test (mergeable: true) while still showing several
    entries here, and that's normal on an actively-developed shared driver.
    """
    url = f"{GERRIT_BASE}/changes/?q=status:open+conflicts:{patch_id}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    if raw.startswith(XSSI_PREFIX):
        raw = raw[len(XSSI_PREFIX):]
    try:
        changes = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return [{"id": c.get("_number"), "subject": c.get("subject", "")} for c in changes]


def fetch_change(patch_id):
    url = f"{GERRIT_BASE}/changes/{patch_id}/detail?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES&o=CURRENT_COMMIT"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"found": False, "error": "not found (404) - may be private, restricted, or wrong id"}
        return {"found": False, "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"found": False, "error": str(e)}

    if raw.startswith(XSSI_PREFIX):
        raw = raw[len(XSSI_PREFIX):]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"found": False, "error": f"bad json: {e}"}

    project = data.get("project", "")
    subject = data.get("subject", "")
    status = data.get("status", "UNKNOWN")
    owner = (data.get("owner") or {}).get("name", "")
    updated = data.get("updated", "")
    created = data.get("created", "")
    submitted = data.get("submitted", "")
    branch = data.get("branch", "")

    labels = data.get("labels", {}) or {}

    def label_summary(label_name):
        lab = labels.get(label_name)
        if not lab:
            return None
        votes = lab.get("all") or []
        cast = [(v.get("name", "?"), v.get("value", 0)) for v in votes if v.get("value")]
        if lab.get("approved"):
            return {"state": "approved", "by": lab["approved"].get("name", ""), "votes": cast}
        if lab.get("rejected"):
            return {"state": "rejected", "by": lab["rejected"].get("name", ""), "votes": cast}
        if cast:
            best = max(cast, key=lambda x: x[1])
            return {"state": f"{best[1]:+d}", "by": best[0], "votes": cast}
        return {"state": "none", "by": "", "votes": []}

    verified = label_summary("Verified")
    code_review = label_summary("Code-Review")

    conflicts = None
    if status == "NEW":
        conflicts = fetch_conflicts(patch_id)

    messages = data.get("messages", []) or []
    latest_comments = []
    # Keep a generous window so filtering CI/Zuul noise out at display time
    # doesn't accidentally bury an older human comment that fell off a short tail.
    for m in messages[-50:]:
        author = (m.get("author") or {}).get("name", "unknown")
        text = (m.get("message") or "").strip().replace("\n", " ")
        if len(text) > 220:
            text = text[:220].rstrip() + "…"
        date = m.get("date", "")
        latest_comments.append({"author": author, "text": text, "date": date})

    return {
        "found": True,
        "project": project,
        "subject": subject,
        "status": status,
        "owner": owner,
        "branch": branch,
        "created": created,
        "updated": updated,
        "submitted": submitted,
        "verified": verified,
        "code_review": code_review,
        "conflicts": conflicts,
        "comments": latest_comments,
        "url": f"{GERRIT_BASE}/c/{project}/+/{patch_id}" if project else f"{GERRIT_BASE}/c/{patch_id}",
    }


def main():
    section_filter = None
    if "--section" in sys.argv:
        idx = sys.argv.index("--section")
        section_filter = sys.argv[idx + 1]

    rows = [r for r in ROWS if r.get("patch_id")]
    if section_filter:
        rows = [r for r in rows if r["section"] == section_filter]

    unique_ids = sorted({r["patch_id"] for r in rows}, key=lambda x: int(re.sub(r"\D", "", x) or 0))
    print(f"Fetching live data for {len(unique_ids)} unique patch id(s)"
          + (f" in section '{section_filter}'" if section_filter else "") + "...")

    cache = {}
    for i, pid in enumerate(unique_ids, 1):
        print(f"  [{i}/{len(unique_ids)}] {pid} ...", end=" ", flush=True)
        result = fetch_change(pid)
        if result.get("found"):
            result["inline_comments"] = fetch_inline_comments(pid)
        cache[pid] = result
        n_inline = len(result.get("inline_comments", []))
        n_conflicts = len(result.get("conflicts") or [])
        conflict_note = f" [{n_conflicts} MERGE CONFLICT{'S' if n_conflicts != 1 else ''}]" if n_conflicts else ""
        print(f"OK ({n_inline} inline comments){conflict_note}" if result.get("found") else f"FAIL ({result.get('error')})")
        time.sleep(0.15)

    found = sum(1 for v in cache.values() if v.get("found"))
    print(f"\nDone: {found}/{len(unique_ids)} resolved to live Gerrit data.")

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "section_filter": section_filter,
        "rows": rows,
        "live": cache,
        "oss_tickets": OSS_TICKETS,
        "red_hat": RED_HAT,
        "section_order": [s for s in SECTION_ORDER if not section_filter or s == section_filter],
    }
    with open("data.json", "w") as f:
        json.dump(out, f, indent=2)
    print("Wrote data.json")


if __name__ == "__main__":
    main()
