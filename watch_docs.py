#!/usr/bin/env python3
"""Watch NRP-CZ/docs for content changes and summarize them with an e-INFRA LLM.

Checks for new commits touching content/ since the last run (state is kept in
state.json, committed back to this repo). For every new commit it fetches the
changed files, asks an LLM at llm.ai.e-infra.cz for a Czech summary, maps the
changed pages to their published URLs at https://nrp-cz.github.io/docs/ and
posts the result as a comment on a digest issue in this repository.

Configuration via environment variables:
  WATCH_REPO        owner/name of the watched repo   (default NRP-CZ/docs)
  WATCH_BRANCH      branch to watch                  (default main)
  WATCH_PATH        path prefix to watch             (default content)
  DIGEST_REPO       owner/name of the repo holding the digest issue
                    (default: GITHUB_REPOSITORY, i.e. this repo)
  DIGEST_ISSUE      fixed issue number to use        (default: newest open
                    issue with the digest label, created if missing)
  E_INFRA_API_TOKEN token for llm.ai.e-infra.cz      (required)
  E_INFRA_MODEL     model name                       (default kimi-k3)
  GH_TOKEN / GITHUB_TOKEN  GitHub API token          (required)

Run `python watch_docs.py --init` once to record the current HEAD without
generating a report.
"""

from __future__ import annotations

import argparse
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

MAX_DIFF_CHARS = 12_000  # per commit, to keep prompts small
MAX_COMMITS_PER_RUN = int(os.environ.get("MAX_COMMITS_PER_RUN", "10"))

PROMPT_TEMPLATE = """\
Jsi asistent, který sleduje vývoj české dokumentace "CESNET Invenio" \
(repozitář {repo}, adresář {path}/). Níže je jeden commit, který dokumentaci mění.

Commit: {sha}
Autor: {author}
Datum: {date}
Zpráva: {message}

Změněné soubory a jejich diff (zkrácený):
{diff}

Napiš stručné shrnutí v češtině (2–4 věty) pro čtenáře, kteří dokumentaci \
používají, ale nejsou její autoři. Popiš, CO se v dokumentaci změnilo nebo \
přibylo (nové téma, přepsaná sekce, opravy, nové obrázky…). Neopakuj commit \
message doslova, nevypisuj názvy souborů ani technické detaily buildu. \
Odpověz pouze samotným shrnutím, bez úvodních frází.\
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


def llm_chat(prompt: str) -> str:
    """Call the e-INFRA LLM (OpenAI-compatible chat completions)."""
    payload = {
        "model": E_INFRA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 400,
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


def format_diff(commit: dict) -> str:
    parts = []
    for f in commit.get("files", []):
        status = {"added": "přidán", "removed": "smazán", "modified": "změněn",
                  "renamed": "přejmenován"}.get(f["status"], f["status"])
        parts.append(f"--- {f['filename']} ({status})")
        patch = f.get("patch")
        if patch:
            parts.append(patch)
    diff = "\n".join(parts)
    return diff[:MAX_DIFF_CHARS] + ("\n… (zkráceno)" if len(diff) > MAX_DIFF_CHARS else "")


# ------------------------------------------------------------------ digest issue

def find_or_create_digest_issue(day: str) -> int:
    """Return the digest issue for the given day (YYYY-MM-DD), creating it.

    One issue per day with changes: "Změny dokumentace NRP-CZ/docs – YYYY-MM-DD".
    Days without changes produce no issue at all.
    """
    title = f"📖 Změny dokumentace NRP-CZ/docs – {day}"

    issues = gh_api(
        f"/repos/{DIGEST_REPO}/issues?state=open&labels={DIGEST_LABEL}&per_page=50"
    )
    for issue in issues:
        if "pull_request" not in issue and issue["title"] == title:
            return issue["number"]

    body = (
        f"Shrnutí změn v adresáři "
        f"[{WATCH_PATH}/](https://github.com/{WATCH_REPO}/tree/{WATCH_BRANCH}/{WATCH_PATH}) "
        f"repozitáře [{WATCH_REPO}](https://github.com/{WATCH_REPO}) "
        f"za den **{day}**.\n\n"
        "Shrnutí generuje LLM na e-INFRA (llm.ai.e-infra.cz) z commitů a diffů; "
        f"odkazy vedou na publikovanou dokumentaci: {DOCS_BASE}/"
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


# ------------------------------------------------------------------ main

def collect_new_commits(since_sha: str | None) -> list[dict]:
    url = f"/repos/{WATCH_REPO}/commits?sha={WATCH_BRANCH}&path={WATCH_PATH}&per_page=100"
    since_date = os.environ.get("SINCE_DATE")  # ISO date, e.g. 2026-08-08
    if since_date:
        url += f"&since={since_date}T00:00:00Z"
    commits = gh_api(url)
    fresh = []
    for c in commits:
        if c["sha"] == since_sha:
            break
        fresh.append(c)
    fresh.reverse()  # oldest first
    return fresh[:MAX_COMMITS_PER_RUN]


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

    head = gh_api(f"/repos/{WATCH_REPO}/commits/{WATCH_BRANCH}")
    head_sha = head["sha"]

    if args.init:
        save_state({"last_sha": head_sha, "updated": datetime.now(timezone.utc).isoformat()})
        print(f"Initialized state at {WATCH_REPO}@{head_sha[:7]}")
        return 0

    # Backfill mode: SINCE_DATE set -> ignore stored state, process by date
    if not os.environ.get("SINCE_DATE") and since_sha == head_sha:
        print("No new commits.")
        return 0

    if not E_INFRA_TOKEN:
        sys.exit("E_INFRA_API_TOKEN is not set")

    fresh = collect_new_commits(since_sha)
    if not fresh:
        # state points at a commit no longer reachable (force push) – reset
        save_state({"last_sha": head_sha, "updated": datetime.now(timezone.utc).isoformat()})
        print("State reset to current HEAD (previous SHA not found in history).")
        return 0

    print(f"{len(fresh)} new commit(s) touching {WATCH_PATH}/")
    issue_numbers: dict[str, int] = {}  # day -> issue number (lazy)

    for c in fresh:
        sha = c["sha"]
        detail = gh_api(f"/repos/{WATCH_REPO}/commits/{sha}")
        message = detail["commit"]["message"].splitlines()[0]
        author = detail["commit"]["author"]["name"]
        date = detail["commit"]["author"]["date"]
        day = date[:10]
        files = detail.get("files", [])

        content_files = [f["filename"] for f in files if is_content_file(f["filename"])]
        if not content_files:
            print(f"  {sha[:7]} – no .md/.mdx changes, skipping LLM")
            continue

        prompt = PROMPT_TEMPLATE.format(
            repo=WATCH_REPO, path=WATCH_PATH, sha=sha[:7], author=author,
            date=date, message=message, diff=format_diff(detail),
        )
        print(f"  {sha[:7]} – summarizing ({len(content_files)} content file(s))…")
        summary = llm_chat(prompt)

        urls = sorted({u for f in content_files if (u := doc_url(f))})
        links_md = "\n".join(f"- 📄 {u}" for u in urls)

        if day not in issue_numbers:
            issue_numbers[day] = find_or_create_digest_issue(day)
        issue_number = issue_numbers[day]

        body = (
            f"### [{message}](https://github.com/{WATCH_REPO}/commit/{sha})\n"
            f"`{sha[:7]}` · {author} · {date[11:16]} UTC\n\n"
            f"{summary}\n\n"
            f"**Publikované stránky:**\n{links_md}"
        )
        post_comment(issue_number, body)
        print(f"  {sha[:7]} – posted to issue #{issue_number} ({day})")

    save_state({
        "last_sha": fresh[-1]["sha"],
        "updated": datetime.now(timezone.utc).isoformat(),
    })
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
