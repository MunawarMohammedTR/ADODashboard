import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

_PR_URL_PATTERN = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", re.IGNORECASE
)
# ADO GitHub integration stores PR links as:
#   vstfs:///GitHub/PullRequest/{connectionGuid}%2F{prNumber}
_VSTFS_PR_PATTERN = re.compile(
    r"vstfs:///GitHub/PullRequest/([^%]+)%2[Ff](\d+)", re.IGNORECASE
)


class GitHubClient:
    def __init__(self, ado_session: requests.Session = None, ado_base: str = ""):
        token = os.environ.get("GITHUB_TOKEN", "")
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Accept"] = "application/vnd.github+json"
        self.session.headers["X-GitHub-Api-Version"] = "2022-11-28"
        self._review_cache: dict[str, bool] = {}
        self._url_cache: dict[str, str | None] = {}
        self._pr_details_cache: dict[str, dict] = {}
        # ADO session for resolving connection GUIDs → GitHub repos
        self._ado_session = ado_session
        self._ado_base = ado_base
        self._connection_cache: dict[str, str] = {}  # guid → "owner/repo"

    def _resolve_connection(self, guid: str) -> str | None:
        """Return 'owner/repo' for a GitHub connection GUID via ADO service endpoints."""
        if guid in self._connection_cache:
            return self._connection_cache[guid]
        if not self._ado_session or not self._ado_base:
            return None
        try:
            url = f"{self._ado_base}/_apis/serviceendpoint/endpoints/{guid}"
            resp = self._ado_session.get(url, params={"api-version": "7.1"}, timeout=15)
            if not resp.ok:
                self._connection_cache[guid] = None
                return None
            data = resp.json()
            # The GitHub URL is in data["url"] e.g. "https://github.com/owner/repo"
            gh_url = data.get("url", "")
            m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", gh_url, re.IGNORECASE)
            if m:
                owner_repo = m.group(1)
                self._connection_cache[guid] = owner_repo
                return owner_repo
        except Exception:
            pass
        self._connection_cache[guid] = None
        return None

    def get_pr_html_url(self, raw_url: str) -> str | None:
        if raw_url in self._url_cache:
            return self._url_cache[raw_url]

        # Direct GitHub URL
        m = _PR_URL_PATTERN.search(raw_url)
        if m:
            result = f"https://github.com/{m.group(1)}/{m.group(2)}/pull/{m.group(3)}"
            self._url_cache[raw_url] = result
            return result

        # vstfs URL — resolve via ADO connection endpoint
        m = _VSTFS_PR_PATTERN.search(raw_url)
        if m:
            guid, pr_num = m.group(1), m.group(2)
            owner_repo = self._resolve_connection(guid)
            if owner_repo:
                result = f"https://github.com/{owner_repo}/pull/{pr_num}"
                self._url_cache[raw_url] = result
                return result

        self._url_cache[raw_url] = None
        return None

    def get_pr_details(self, raw_url: str) -> dict | None:
        """Return PR metadata dict or None if the PR cannot be resolved/fetched."""
        if raw_url in self._pr_details_cache:
            return self._pr_details_cache[raw_url]

        pr_url = self.get_pr_html_url(raw_url)
        if not pr_url:
            self._pr_details_cache[raw_url] = None
            return None

        m = _PR_URL_PATTERN.search(pr_url)
        if not m:
            self._pr_details_cache[raw_url] = None
            return None

        owner, repo, pr_num = m.group(1), m.group(2), m.group(3)
        try:
            resp = self.session.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}",
                timeout=15,
            )
            if resp.status_code == 404:
                self._pr_details_cache[raw_url] = None
                return None
            resp.raise_for_status()
            d = resp.json()
            merged = bool(d.get("merged_at"))
            base_ref = (d.get("base") or {}).get("ref", "")
            result = {
                "number": d.get("number"),
                "title": d.get("title", ""),
                "state": d.get("state", ""),
                "merged": merged,
                "base_ref": base_ref,
                "merged_to_master": merged and base_ref in ("main", "master"),
                "author_login": (d.get("user") or {}).get("login", ""),
                "html_url": d.get("html_url", pr_url),
            }
        except requests.RequestException:
            result = None

        self._pr_details_cache[raw_url] = result
        return result

    def get_prs_for_sprint(self, owner_repo: str, start: str, finish: str) -> list[dict]:
        """Fetch all PRs from owner_repo whose created_at falls within [start, finish].

        Uses GitHub Search API (no pagination > 1000 results expected for a sprint).
        Returns list of dicts: {number, title, state, merged, merged_to_master,
                                author_login, html_url, created_at}.
        """
        if not start or not finish:
            return []
        owner_repo = owner_repo.strip()
        results = []
        page = 1
        while True:
            try:
                resp = self.session.get(
                    "https://api.github.com/search/issues",
                    params={
                        "q": f"repo:{owner_repo} is:pr created:{start[:10]}..{finish[:10]}",
                        "per_page": 100,
                        "page": page,
                    },
                    timeout=20,
                )
                if not resp.ok:
                    print(f"  [GitHub] PR search failed: {resp.status_code} {resp.text[:120]}")
                    break
                data = resp.json()
                items = data.get("items", [])
                if not items:
                    break
                _AI_LABEL_RE = re.compile(r"ai[\s_-]*(generated|assisted|powered)", re.IGNORECASE)
                for item in items:
                    pr_url = item.get("pull_request", {}).get("html_url") or item.get("html_url", "")
                    merged_at = item.get("pull_request", {}).get("merged_at")
                    merged = bool(merged_at)
                    # Determine base branch by fetching individual PR (needed for merge target)
                    base_ref = ""
                    if pr_url:
                        details = self.get_pr_details(pr_url)
                        if details:
                            base_ref = details.get("base_ref", "")
                            merged = details.get("merged", merged)
                    label_names = [lbl.get("name", "") for lbl in (item.get("labels") or [])]
                    ai_labeled = any(_AI_LABEL_RE.search(lbl) for lbl in label_names)
                    results.append({
                        "number": item.get("number"),
                        "title": item.get("title", ""),
                        "state": item.get("state", ""),
                        "merged": merged,
                        "base_ref": base_ref,
                        "merged_to_master": merged and base_ref in ("main", "master"),
                        "author_login": (item.get("user") or {}).get("login", ""),
                        "html_url": pr_url,
                        "created_at": item.get("created_at", ""),
                        "ai_labeled": ai_labeled,
                        "labels": label_names,
                    })
                if len(items) < 100:
                    break
                page += 1
            except requests.RequestException as exc:
                print(f"  [GitHub] PR search error: {exc}")
                break
        return results

    def has_approved_review(self, raw_url: str) -> bool:
        if raw_url in self._review_cache:
            return self._review_cache[raw_url]

        pr_url = self.get_pr_html_url(raw_url)
        if not pr_url:
            self._review_cache[raw_url] = False
            return False

        m = _PR_URL_PATTERN.search(pr_url)
        if not m:
            self._review_cache[raw_url] = False
            return False

        owner, repo, pr_num = m.group(1), m.group(2), m.group(3)
        try:
            resp = self.session.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}/reviews",
                timeout=15,
            )
            if resp.status_code == 404:
                self._review_cache[raw_url] = False
                return False
            resp.raise_for_status()
            approved = any(r.get("state") == "APPROVED" for r in resp.json())
        except requests.RequestException:
            approved = False

        self._review_cache[raw_url] = approved
        return approved
