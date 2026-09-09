#!/usr/bin/env python3
"""Watch NRP-CZ/docs for content changes and summarize them with an e-INFRA LLM.

Hybrid PR-based watcher. It detects new merged pull requests that touch
content/ (one comment per PR, using the PR title/body and the full PR diff)
plus any direct commits to the watched branch that are not part of a PR
(fallback, so nothing is missed). Each change is summarized in English by an
LLM at llm.ai.e-infra.cz, mapped to its published URLs at
https://nrp-cz.github.io/docs/ and posted as a comment on a daily digest issue
in this repository.

Configuration via environment variables:
  WATCH_REPO        owner/name of the watched repo   (default NRP-CZ/docs)
  WATCH_BRANCH      branch to watch                  (default main)
  WATCH_PATH        path prefix to watch             (default content)
  DIGEST_REPO       owner/name of the repo holding the digest issue
                    (default: GITHUB_REPOSITORY, i.e. this repo)
  E_INFRA_API_TOKEN token for llm.ai.e-infra.cz      (required)
  E_INFRA_MODEL     model name                       (default kimi-k3)
  GH_TOKEN / GITHUB_TOKEN  GitHub API token          (required)

Run `python watch_docs.py --init` once to record the current HEAD without
generating a report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WATCH_REPO = os.environ.get("WATCH_REPO", "NRP-CZ/docs")
WATCH_BRANCH = os.environ.get("WATCH_BRANCH", "main")
WATCH_PATH = os.environ.get("WATCH_PATH", "content")
DIGEST_REPO = os.environ.get("DIGEST_REPO", os.environ.get("GITHUB_REPOSITORY", ""))
DIGEST_LABEL = "docs-digest"
DOCS_BASE = "https://nrp-cz.github.io/docs"

E_INFRA_URL = os.environ.get(
    "E_INFRA_URL", "https://llm.ai.e-infra.cz/v1/chat/completions"
)
E_INFRA_MODEL = os.environ.get("E_INFRA_MODEL", "kimi-k3")
E_INFRA_TOKEN = os.environ.get("E_INFRA_API_TOKEN", "")

GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
STATE_FILE = Path(__file__).parent / "state.json"

MAX_DIFF_CHARS = 12_000  # per change, to keep prompts small
MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "10"))

PROMPT_TEMPLATE = """\
You are an assistant tracking changes in the "CESNET Invenio" documentation \
(repository {repo}, directory {path}/). Below is one change to the docs.

Title: {title}
Author: {author}
Date: {date}
Description: {description}

Changed files and their diff (truncated):
{diff}

Summarize the change in English for readers who use the documentation but are \
not its authors. Follow these rules strictly:

1. First line: `## <topic>` — a short human-readable name of the area the \
change affects (e.g. "Search", "FAQ", "Workflows"). Not a file name.
2. Below it, bullet points (`-`), never continuous paragraphs.
3. Each bullet = one distinct change/fact; typically 2–5 bullets.
4. Be concise and factual — what changed or was added. No introductory phrases \
like "The documentation now…" or "The page was extended…".
5. For large changes (e.g. a big FAQ expansion) do NOT list everything — \
summarize in one bullet plus a few representative examples ending with "etc." \
(e.g. "New Q&As added, e.g. hierarchical records, ORCID validation, … etc.").
6. Keep technical details (field names, keys, paths) in `backticks`, but only \
what is needed to understand the change.
7. Reply with the content only (heading + bullets), no opening or closing \
remarks.\
"""

GROUP_PROMPT_TEMPLATE = """\
You are an assistant tracking changes in the "CESNET Invenio" documentation \
(repository {repo}, directory {path}/). Below are {count} changes to the docs \
that all affect the same page(s). They were merged on the same day.

{changes}

Summarize the combined effect of all these changes in English for readers who \
use the documentation but are not its authors. Follow these rules strictly:

1. First line: `## <topic>` — a short human-readable name of the area the \
changes affect (e.g. "Search", "FAQ", "Workflows"). Not a file name.
2. Below it, bullet points (`-`), never continuous paragraphs.
3. Each bullet = one distinct change/fact; typically 2–6 bullets.
4. Be concise and factual — what changed or was added overall. No introductory \
phrases like "The documentation now…" or "The page was extended…".
5. Do NOT repeat the same fact for each change — merge overlapping changes \
into one bullet. For large changes do NOT list everything — summarize in one \
bullet plus a few representative examples ending with "etc.".
6. Keep technical details (field names, keys, paths) in `backticks`, but only \
what is needed to understand the change.
7. Reply with the content only (heading + bullets), no opening or closing \
remarks.\
"""


# ---------------------------------------------------------------- HTTP helpers

def gh_api(path: str, method: str = "GET", payload: dict | None = None):
    """Call the GitHub REST API."""
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "nrp-docs-watch")
    data = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        raise RuntimeError(f"GitHub API {method} {url} -> {e.code}: {detail}") from e


def llm_chat(prompt: str, max_tokens: int = 400) -> str:
    """Call the e-INFRA LLM (OpenAI-compatible chat completions)."""
    payload = {
        "model": E_INFRA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(E_INFRA_URL, method="POST")
    req.add_header("Authorization", f"Bearer {E_INFRA_TOKEN}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "nrp-docs-watch")
    data = json.dumps(payload).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, data=data, timeout=300) as resp:
                body = json.loads(resp.read().decode())
                return body["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as e:
            if attempt == 2:
                raise
            wait = 2 ** attempt * 5
            print(f"  LLM call failed ({e}), retrying in {wait}s…", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


# ------------------------------------------------------------------ state

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ------------------------------------------------------------------ helpers

def doc_url(filename: str) -> str | None:
    """Map a content/ file to its published Nextra URL, or None."""
    if not filename.startswith(WATCH_PATH + "/"):
        return None
    rel = filename[len(WATCH_PATH) + 1:]
    stem, dot, _ext = rel.rpartition(".")
    if not dot:
        return None
    if stem.endswith("/index") or stem == "index":
        stem = stem[: -len("index")].rstrip("/")
    return f"{DOCS_BASE}/{stem}" if stem else f"{DOCS_BASE}/"


def is_content_file(filename: str) -> bool:
    return filename.startswith(WATCH_PATH + "/") and filename.rpartition(".")[2] in (
        "md", "mdx",
    )


def _diff_anchor(filename: str) -> str:
    """GitHub anchors per-file diffs on a commit page as #diff-<sha256(path)[:32]>."""
    return hashlib.sha256(filename.encode()).hexdigest()[:32]


def format_diff(files: list[dict]) -> str:
    parts = []
    for f in files:
        status = {"added": "added", "removed": "removed", "modified": "modified",
                  "renamed": "renamed"}.get(f["status"], f["status"])
        parts.append(f"--- {f['filename']} ({status})")
        patch = f.get("patch")
        if patch:
            parts.append(patch)
    diff = "\n".join(parts)
    return diff[:MAX_DIFF_CHARS] + ("\n… (truncated)" if len(diff) > MAX_DIFF_CHARS else "")


def content_files_of(files: list[dict]) -> list[str]:
    return [f["filename"] for f in files if is_content_file(f["filename"])]


def pr_commits(pr_number: int) -> list[str]:
    """SHAs of the commits in a pull request."""
    commits = gh_api(f"/repos/{WATCH_REPO}/pulls/{pr_number}/commits?per_page=100")
    return [c["sha"] for c in commits]


def pr_files(pr_number: int) -> list[dict]:
    """Files changed by a pull request (with patches)."""
    return gh_api(f"/repos/{WATCH_REPO}/pulls/{pr_number}/files?per_page=100")


def commit_files(sha: str) -> list[dict]:
    """Files changed by a single commit (with patches)."""
    return gh_api(f"/repos/{WATCH_REPO}/commits/{sha}").get("files", [])


# ------------------------------------------------------------------ digest issue

def find_or_create_digest_issue(day: str) -> int:
    """Return the digest issue for the given day (YYYY-MM-DD), creating it.

    One issue per day with changes: "CESNET Invenio docs changes – YYYY-MM-DD".
    Days without changes produce no issue at all.
    """
    title = f"📖 CESNET Invenio docs changes – {day}"

    issues = gh_api(
        f"/repos/{DIGEST_REPO}/issues?state=open&labels={DIGEST_LABEL}&per_page=50"
    )
    for issue in issues:
        if "pull_request" not in issue and issue["title"] == title:
            return issue["number"]

    body = (
        f"Summaries of changes in the "
        f"[{WATCH_PATH}/](https://github.com/{WATCH_REPO}/tree/{WATCH_BRANCH}/{WATCH_PATH}) "
        f"directory of [{WATCH_REPO}](https://github.com/{WATCH_REPO}) "
        f"for **{day}**.\n\n"
        "Summaries are generated by an LLM at e-INFRA (llm.ai.e-infra.cz) from "
        f"commits and diffs; links point to the published documentation: {DOCS_BASE}/"
    )
    issue = gh_api(
        f"/repos/{DIGEST_REPO}/issues",
        method="POST",
        payload={"title": title, "body": body, "labels": [DIGEST_LABEL]},
    )
    print(f"Created digest issue #{issue['number']} ({day}) in {DIGEST_REPO}")
    return issue["number"]


def post_comment(issue_number: int, body: str) -> None:
    gh_api(
        f"/repos/{DIGEST_REPO}/issues/{issue_number}/comments",
        method="POST",
        payload={"body": body},
    )


# ------------------------------------------------------------------ collection

def collect_merged_prs(since_date: str | None) -> list[dict]:
    """Merged PRs touching WATCH_PATH, oldest first (no cutoff)."""
    url = f"/repos/{WATCH_REPO}/pulls?state=closed&base={WATCH_BRANCH}&sort=updated&direction=desc&per_page=100"
    prs = gh_api(url)
    merged = [p for p in prs if p.get("merged_at")]
    if since_date:
        merged = [p for p in merged if p["merged_at"][:10] >= since_date]
    merged.reverse()  # oldest first
    return merged


def group_by_pages(items: list[dict]) -> list[list[dict]]:
    """Group changes by identical set of published page URLs.

    Items with the same set of doc URLs (e.g. all touching only
    sensitive_data) are grouped together; items with different or partially
    overlapping page sets stay separate. Order within a group is preserved.
    """
    groups: dict[frozenset, list[dict]] = {}
    order: list[frozenset] = []
    for it in items:
        key = frozenset(it["urls"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(it)
    return [groups[k] for k in order]


def collect_direct_commits(since_sha: str | None, pr_sha_set: set[str]) -> list[dict]:
    """Commits on the branch touching WATCH_PATH that are not part of any PR.

    Merge commits (e.g. "Merge pull request #N") are excluded — they are
    already covered by the PR they merge. No cutoff here; the caller applies
    the combined limit.
    """
    url = f"/repos/{WATCH_REPO}/commits?sha={WATCH_BRANCH}&path={WATCH_PATH}&per_page=100"
    since_date = os.environ.get("SINCE_DATE")
    if since_date:
        commits = gh_api(url + f"&since={since_date}T00:00:00Z")
    else:
        commits = gh_api(url)
        fresh = []
        for c in commits:
            if c["sha"] == since_sha:
                break
            fresh.append(c)
        commits = fresh
    direct = []
    for c in commits:
        if c["sha"] in pr_sha_set:
            continue
        msg = c["commit"]["message"]
        if msg.startswith("Merge pull request") or msg.startswith("Merge branch"):
            continue
        direct.append(c)
    direct.reverse()  # oldest first
    return direct


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--init", action="store_true",
        help="record current HEAD of the watched repo and exit (no report)",
    )
    args = parser.parse_args()

    if not GH_TOKEN:
        sys.exit("GH_TOKEN/GITHUB_TOKEN is not set")

    state = load_state()
    since_sha = state.get("last_sha")
    since_date = os.environ.get("SINCE_DATE")  # backfill mode

    head = gh_api(f"/repos/{WATCH_REPO}/commits/{WATCH_BRANCH}")
    head_sha = head["sha"]

    if args.init:
        save_state({"last_sha": head_sha, "updated": datetime.now(timezone.utc).isoformat()})
        print(f"Initialized state at {WATCH_REPO}@{head_sha[:7]}")
        return 0

    if not since_date and since_sha == head_sha:
        print("No new changes.")
        return 0

    if not E_INFRA_TOKEN:
        sys.exit("E_INFRA_API_TOKEN is not set")

    # --- collect merged PRs and direct commits -------------------------------
    prs = collect_merged_prs(since_date)
    pr_sha_set: set[str] = set()
    for p in prs:
        pr_sha_set.update(pr_commits(p["number"]))

    direct = collect_direct_commits(since_sha, pr_sha_set)

    # combined limit: keep the oldest MAX_ITEMS_PER_RUN changes overall
    if len(prs) + len(direct) > MAX_ITEMS_PER_RUN:
        # drop the newest (end of the oldest-first lists) to respect the limit
        prs = prs[:MAX_ITEMS_PER_RUN]
        direct = direct[:MAX_ITEMS_PER_RUN - len(prs)]

    total = len(prs) + len(direct)
    if total == 0:
        if since_date:
            print("No changes in the requested period.")
        else:
            save_state({"last_sha": head_sha, "updated": datetime.now(timezone.utc).isoformat()})
            print("State reset to current HEAD (previous SHA not found in history).")
        return 0

    print(f"{total} change(s): {len(prs)} merged PR(s), {len(direct)} direct commit(s)")
    issue_numbers: dict[str, int] = {}  # day -> issue number (lazy)

    # --- build a uniform list of changes -------------------------------------
    changes: list[dict] = []

    for p in prs:
        number = p["number"]
        files = pr_files(number)
        content_files = content_files_of(files)
        if not content_files:
            print(f"  PR #{number} – no .md/.mdx changes, skipping LLM")
            continue
        changes.append({
            "kind": "pr",
            "number": number,
            "title": p["title"],
            "author": p["user"]["login"],
            "date": p["merged_at"],
            "day": p["merged_at"][:10],
            "description": (p.get("body") or "").strip() or "(no description)",
            "files": files,
            "content_files": content_files,
            "urls": sorted({u for f in content_files if (u := doc_url(f))}),
        })

    for c in direct:
        sha = c["sha"]
        detail = gh_api(f"/repos/{WATCH_REPO}/commits/{sha}")
        files = detail.get("files", [])
        content_files = content_files_of(files)
        if not content_files:
            print(f"  {sha[:7]} – no .md/.mdx changes, skipping LLM")
            continue
        changes.append({
            "kind": "commit",
            "sha": sha,
            "title": detail["commit"]["message"].splitlines()[0],
            "author": detail["commit"]["author"]["name"],
            "date": detail["commit"]["author"]["date"],
            "day": detail["commit"]["author"]["date"][:10],
            "description": "(direct commit)",
            "files": files,
            "content_files": content_files,
            "urls": sorted({u for f in content_files if (u := doc_url(f))}),
        })

    # --- group by day, then by identical page set ----------------------------
    by_day: dict[str, list[dict]] = {}
    for ch in changes:
        by_day.setdefault(ch["day"], []).append(ch)

    for day in sorted(by_day):
        for group in group_by_pages(by_day[day]):
            if day not in issue_numbers:
                issue_numbers[day] = find_or_create_digest_issue(day)
            issue_number = issue_numbers[day]

            if len(group) == 1:
                _post_single(issue_number, group[0])
            else:
                _post_group(issue_number, group)

    save_state({
        "last_sha": head_sha,
        "updated": datetime.now(timezone.utc).isoformat(),
    })
    print("Done.")
    return 0


def _post_single(issue_number: int, ch: dict) -> None:
    """Post one change as its own comment."""
    prompt = PROMPT_TEMPLATE.format(
        repo=WATCH_REPO, path=WATCH_PATH, title=ch["title"], author=ch["author"],
        date=ch["date"], description=ch["description"], diff=format_diff(ch["files"]),
    )
    print(f"  {ch['kind']} {ch.get('number', ch.get('sha', '')[:7])} – summarizing…")
    summary = llm_chat(prompt)

    links_md = "\n".join(f"- 📄 {u}" for u in ch["urls"])
    if ch["kind"] == "pr":
        number = ch["number"]
        diffs_md = " · ".join(
            f"[`{f.split('/')[-1]}`](https://github.com/{WATCH_REPO}/pull/{number}/files#diff-{_diff_anchor(f)})"
            for f in ch["content_files"]
        )
        body = (
            f"### [PR #{number}: {ch['title']}](https://github.com/{WATCH_REPO}/pull/{number})\n"
            f"by {ch['author']} · merged {ch['date'][:10]}\n\n"
            f"{summary}\n\n"
            f"**Published pages:**\n{links_md}\n\n"
            f"<sub>Diffs: {diffs_md} · "
            f"[whole PR](https://github.com/{WATCH_REPO}/pull/{number}/files)</sub>"
        )
    else:
        sha = ch["sha"]
        diffs_md = " · ".join(
            f"[`{f.split('/')[-1]}`](https://github.com/{WATCH_REPO}/commit/{sha}#diff-{_diff_anchor(f)})"
            for f in ch["content_files"]
        )
        body = (
            f"### [{ch['title']}](https://github.com/{WATCH_REPO}/commit/{sha})\n"
            f"`{sha[:7]}` · {ch['author']} · {ch['date'][11:16]} UTC\n\n"
            f"{summary}\n\n"
            f"**Published pages:**\n{links_md}\n\n"
            f"<sub>Diffs: {diffs_md} · "
            f"[whole commit](https://github.com/{WATCH_REPO}/commit/{sha})</sub>"
        )
    post_comment(issue_number, body)
    print(f"  {ch['kind']} {ch.get('number', ch.get('sha', '')[:7])} – posted to issue #{issue_number}")


def _post_group(issue_number: int, group: list[dict]) -> None:
    """Post several changes touching the same page(s) as one combined comment.

    If the LLM returns an empty/too-short summary for the combined prompt, fall
    back to posting each change individually so nothing is lost.
    """
    changes_block = []
    for ch in group:
        label = f"PR #{ch['number']}" if ch["kind"] == "pr" else ch["sha"][:7]
        changes_block.append(
            f"Change {label} — {ch['title']}\n"
            f"Author: {ch['author']}\n"
            f"Description: {ch['description']}\n"
            f"Diff:\n{format_diff(ch['files'])}"
        )
    prompt = GROUP_PROMPT_TEMPLATE.format(
        repo=WATCH_REPO, path=WATCH_PATH, count=len(group),
        changes="\n\n".join(changes_block),
    )
    print(f"  group of {len(group)} changes ({group[0]['urls'][0]}) – summarizing…")
    summary = llm_chat(prompt, max_tokens=800)
    if len(summary.strip()) < 20:
        print(f"  group summary empty ({len(summary.strip())} chars) – falling back to individual posts")
        for ch in group:
            _post_single(issue_number, ch)
        return

    links_md = "\n".join(f"- 📄 {u}" for u in group[0]["urls"])

    refs = []
    for ch in group:
        if ch["kind"] == "pr":
            refs.append(f"[PR #{ch['number']}](https://github.com/{WATCH_REPO}/pull/{ch['number']})")
        else:
            refs.append(f"[`{ch['sha'][:7]}`](https://github.com/{WATCH_REPO}/commit/{ch['sha']})")
    refs_md = " · ".join(refs)

    authors = sorted({ch["author"] for ch in group})
    body = (
        f"### {group[0]['urls'][0].rsplit('/', 1)[-1]} — {len(group)} changes\n"
        f"by {', '.join(authors)} · merged {group[0]['day']}\n\n"
        f"{summary}\n\n"
        f"**Published pages:**\n{links_md}\n\n"
        f"<sub>Changes: {refs_md}</sub>"
    )
    post_comment(issue_number, body)
    print(f"  group of {len(group)} changes – posted to issue #{issue_number}")


if __name__ == "__main__":
    sys.exit(main())
