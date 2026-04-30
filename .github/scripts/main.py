import asyncio
import logging
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx
from gql import Client, gql
from gql.transport.httpx import HTTPXAsyncTransport

ORG = "HertzifyOS"
OTA_REPO = "OTA"
MANIFEST_REPO = "android_manifest"
SNIPPETS_FILE = "hertzify"
CHANGELOG_PATH = "changelogs/source.txt"
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
RAW_BASE = f"https://raw.githubusercontent.com/{ORG}"
EXCLUDE_REPOS = {"OTA", "android_manifest", "hertzifyos-web"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value

BRANCH       = require_env("BRANCH")
GITHUB_TOKEN = require_env("GITHUB_TOKEN")
INPUT_DATE   = require_env("START_DATE")


def fetch_existing_changelog() -> str:
    url = f"{RAW_BASE}/{OTA_REPO}/main/{CHANGELOG_PATH}"
    log.info("Fetching existing changelog from %s", url)
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        if r.status_code == 404:
            log.warning("No existing changelog found, will create new.")
            return ""
        r.raise_for_status()
        return r.text
    except httpx.HTTPError as e:
        log.error("Failed to fetch existing changelog: %s", e)
        sys.exit(1)


def get_projects(file: str) -> list[str]:
    url = f"{RAW_BASE}/{MANIFEST_REPO}/{BRANCH}/snippets/{file}.xml"
    log.info("Fetching manifest from %s", url)
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("Failed to fetch manifest: %s", e)
        sys.exit(1)

    root = ET.fromstring(r.content)
    projects = [
        p.get("name", "").split("/")[-1]
        for p in root.findall(".//project")
        if p.get("remote") == "hertzify"
        and p.get("name", "")
        and p.get("name", "").split("/")[-1] not in EXCLUDE_REPOS
    ]
    log.info("Found %d projects in manifest", len(projects))
    return projects


COMMIT_QUERY = gql("""
query ($org: String!, $repo: String!, $branch: String!, $cursor: String) {
  repository(owner: $org, name: $repo) {
    ref(qualifiedName: $branch) {
      target {
        ... on Commit {
          history(first: 100, after: $cursor) {
            pageInfo {
              hasNextPage
              endCursor
            }
            edges {
              node {
                oid
                messageHeadline
                committedDate
                author { name }
                url
              }
            }
          }
        }
      }
    }
  }
}
""")


async def fetch_commits_for_repo(
    client: Client,
    repo: str,
    start_date: datetime,
    end_date: datetime,
) -> list[dict]:
    commits = []
    cursor = None

    while True:
        variables = {
            "org": ORG,
            "repo": repo,
            "branch": BRANCH,
            "cursor": cursor,
        }
        try:
            result = await client.execute(COMMIT_QUERY, variable_values=variables)
        except Exception as e:
            log.warning("Error fetching %s: %s", repo, e)
            break

        repository = result.get("repository")
        if not repository or not repository.get("ref"):
            log.warning("Branch '%s' not found in %s — skipping", BRANCH, repo)
            break

        history = repository["ref"]["target"]["history"]
        edges = history.get("edges", [])
        if not edges:
            break

        stop = False
        for edge in edges:
            node = edge["node"]
            commit_date = datetime.strptime(
                node["committedDate"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)

            if commit_date < start_date:
                stop = True
                break

            if commit_date <= end_date:
                commits.append({
                    "repo":   repo,
                    "hash":   node["oid"],
                    "link":   node["url"],
                    "title":  node["messageHeadline"],
                    "author": node["author"]["name"],
                    "date":   node["committedDate"],
                })

        page_info = history["pageInfo"]
        if stop or not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    log.info("%-40s → %d commits", repo, len(commits))
    return commits


async def fetch_all_commits(
    repos: list[str],
    start_date: datetime,
    end_date: datetime,
) -> list[dict]:
    transport = HTTPXAsyncTransport(
        url=GITHUB_GRAPHQL_URL,
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
    )
    async with Client(transport=transport, fetch_schema_from_transport=False) as session:
        results = await asyncio.gather(*[
            fetch_commits_for_repo(session, repo, start_date, end_date)
            for repo in repos
        ])
    return [c for repo_commits in results for c in repo_commits]


def render_new_block(commits: list[dict]) -> str:
    commits.sort(key=lambda x: (x["date"][:10], x["repo"]), reverse=True)

    lines = []
    current_date = None
    current_repo = None

    for commit in commits:
        date = commit["date"][:10]
        repo = commit["repo"]

        if date != current_date:
            if current_date is not None:
                lines.append("")
            lines.append(f"=== {date}  ===")
            current_date = date
            current_repo = None

        if repo != current_repo:
            lines.append(f"\n[{repo}]")
            current_repo = repo

        lines.append(
            f"- {commit['title']} "
            f"({commit['hash'][:7]}) "
            f"by {commit['author']}"
        )

    return "\n".join(lines)


def merge_changelog(existing: str, new_block: str) -> str:
    existing = existing.strip()
    new_block = new_block.strip()
    if not existing:
        return new_block
    return new_block + "\n\n" + existing


async def main() -> None:
    try:
        start_date = datetime.strptime(INPUT_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        log.error("Invalid START_DATE format. Expected YYYY-MM-DD, got: %s", INPUT_DATE)
        sys.exit(1)

    end_date = datetime.now(timezone.utc)
    log.info("Collecting commits from %s → %s on branch '%s'", INPUT_DATE, end_date.date(), BRANCH)

    repos = get_projects(SNIPPETS_FILE)
    if not repos:
        log.error("No repositories found in manifest. Aborting.")
        sys.exit(1)

    existing_changelog = fetch_existing_changelog()

    all_commits = await fetch_all_commits(repos, start_date, end_date)
    log.info("Total commits collected: %d", len(all_commits))

    if not all_commits:
        log.warning("No new commits found in the given date range. Nothing to write.")
        sys.exit(0)

    new_block = render_new_block(all_commits)
    final_content = merge_changelog(existing_changelog, new_block)

    out_path = Path(CHANGELOG_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(final_content + "\n", encoding="utf-8")
    log.info("Written to %s", CHANGELOG_PATH)


if __name__ == "__main__":
    asyncio.run(main())