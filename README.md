# OpenStack Hibiscus Patch Dashboard

Generates a live status dashboard for the Dell storage driver patches tracked
for the OpenStack 2026.2 Hibiscus release, pulling status/review data from
opendev.org's public Gerrit REST API.

Patch list source: the "OpenStack deliverables" Confluence page
(confluence.cec.lab.emc.com/spaces/OSE/pages/1219228298), 2026.2 Hibiscus
section only.

## Files

- `dataset.py` — the hand-curated patch list (release section, patch ID,
  storage platform, component, owner, etc.), transcribed from the Confluence
  export. Update this by hand when the source page's patch list changes.
- `fetch_gerrit.py` — fetches live status, votes, top-level messages, and
  inline review comments from review.opendev.org for every patch in
  `dataset.py`, writes `data.json`.
- `build.py` — reads `data.json`, infers an "action needed from us" verdict
  per patch (Gerrit's own `unresolved` flag on inline comment threads is the
  primary signal, with CI/bot/bookkeeping noise filtered out), and renders
  `dashboard.html` — a self-contained static page (Artifact-ready).

## Usage

```
python3 fetch_gerrit.py --section "2026.2 Hibiscus"
python3 build.py
```

Then publish `dashboard.html` via the Artifact tool. `data.json` and
`dashboard.html` are gitignored — they're regenerated outputs, not source.

## Scheduled routine

A weekly cloud routine clones this repo, runs the two commands above, and
republishes to the existing dashboard artifact URL (kept stable via the
Artifact tool's `url` parameter — republishing the same file path from a
prior session's URL updates that artifact rather than creating a new one).
It also drafts a short email summary of the week's changes.
