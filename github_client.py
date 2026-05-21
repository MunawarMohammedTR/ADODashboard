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
