import base64
import os
import re
import time

import requests
from dotenv import load_dotenv

load_dotenv()

_WORK_ITEM_FIELDS = [
    "System.Id",
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.AssignedTo",
    "System.AreaPath",
    "Microsoft.VSTS.Scheduling.StoryPoints",
    "Microsoft.VSTS.Common.AcceptanceCriteria",
]

_GITHUB_PR_RELATION = "GitHub Pull Request"
_GITHUB_COMMIT_RELATION = "GitHub Commit"


def _extract_org(value: str) -> str:
    """Accept either 'myorg' or 'https://dev.azure.com/myorg[/...]'."""
    m = re.match(r"https?://dev\.azure\.com/([^/]+)", value.strip())
    return m.group(1) if m else value.strip().rstrip("/")


def _extract_project(value: str) -> str:
    """Accept either 'MyProject' or 'https://dev.azure.com/org/MyProject[/...]'."""
    m = re.match(r"https?://dev\.azure\.com/[^/]+/([^/]+)", value.strip())
    return m.group(1) if m else value.strip().rstrip("/")


def _extract_team_name(value: str) -> str:
    """Accept a plain team name or a board URL; extract the team name segment (keep dashes)."""
    m = re.search(r"/_boards/board/t/([^/]+)/", value)
    return m.group(1) if m else value.strip()


def _normalize(name: str) -> str:
    """Lowercase + collapse dashes, underscores, spaces for fuzzy matching."""
    return re.sub(r"[-_\s]+", " ", name).strip().lower()


class AzureDevOpsClient:
    def __init__(self):
        org = _extract_org(os.environ["ADO_ORG"])
        project = _extract_project(os.environ["ADO_PROJECT"])
        pat = os.environ.get("ADO_PAT", "")
        if not pat or pat == "your-pat-token":
            raise ValueError(
                "ADO_PAT is not set. Edit your .env file and add a valid Personal Access Token.\n"
                "  Create one at: https://dev.azure.com/<org>/_usersSettings/tokens\n"
                "  Required scopes: Work Items (Read), Project and Team (Read)"
            )
        self.project = project
        self.base = f"https://dev.azure.com/{org}"
        self.proj_base = f"{self.base}/{project}"
        token = base64.b64encode(f":{pat}".encode()).decode()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        print(f"  ADO base URL : {self.base}")
        print(f"  ADO project  : {self.project}")

    def _parse_json(self, resp: requests.Response) -> dict:
        content_type = resp.headers.get("Content-Type", "")
        if "json" not in content_type:
            preview = resp.text[:600].replace("\n", " ").replace("\r", "")
            raise ValueError(
                f"ADO returned non-JSON response (HTTP {resp.status_code}).\n"
                f"  URL: {resp.url}\n"
                f"  Content-Type: {content_type}\n"
                f"  Body preview: {preview!r}\n\n"
                "This usually means authentication failed or the URL is wrong.\n"
                "Check that ADO_PAT is valid and ADO_ORG / ADO_PROJECT are correct."
            )
        try:
            return resp.json()
        except Exception as exc:
            preview = resp.text[:600]
            raise ValueError(
                f"Failed to parse ADO JSON response (HTTP {resp.status_code}).\n"
                f"  URL: {resp.url}\n"
                f"  Body: {preview!r}"
            ) from exc

    def _get(self, url: str, params: dict = None) -> dict:
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                return self._parse_json(resp)
            except requests.exceptions.ReadTimeout:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def _post(self, url: str, payload: dict, params: dict = None) -> dict:
        for attempt in range(3):
            try:
                resp = self.session.post(url, json=payload, params=params, timeout=30)
                resp.raise_for_status()
                return self._parse_json(resp)
            except requests.exceptions.ReadTimeout:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def get_teams(self) -> list[dict]:
        url = f"{self.base}/_apis/projects/{self.project}/teams"
        teams = []
        skip = 0
        page = 200
        while True:
            data = self._get(url, params={"api-version": "7.1", "$top": page, "$skip": skip})
            batch = data.get("value", [])
            teams.extend(batch)
            if len(batch) < page:
                break
            skip += page

        teams_filter_raw = os.environ.get("TEAMS_FILTER", "").strip()
        if teams_filter_raw:
            allowed_norm = {
                _normalize(_extract_team_name(t))
                for t in teams_filter_raw.split(",")
                if t.strip()
            }
            filtered = [t for t in teams if _normalize(t["name"]) in allowed_norm]
            if not filtered:
                all_names = [t["name"] for t in teams]
                print(
                    f"\nWARNING: TEAMS_FILTER matched 0 of {len(teams)} teams.\n"
                    f"  Filter resolved to: {allowed_norm}\n"
                    f"  Available teams (first 20): {all_names[:20]}\n"
                    "  Update TEAMS_FILTER in .env to match one of the above names.\n"
                )
            teams = filtered if filtered else teams

        return [{"id": t["id"], "name": t["name"]} for t in teams]

    def _get_all_iterations(self, team_id: str) -> list[dict]:
        """Fetch every iteration assigned to the team (no timeframe filter)."""
        url = f"{self.proj_base}/{team_id}/_apis/work/teamsettings/iterations"
        try:
            resp = self.session.get(url, params={"api-version": "7.1"}, timeout=30)
            if resp.status_code in (400, 404):
                return []
            resp.raise_for_status()
            return self._parse_json(resp).get("value", [])
        except requests.exceptions.HTTPError:
            return []

    def get_sprints(self, team_id: str, count: int, start_sprint: str = "") -> list[dict]:
        """Return up to count sprints sorted oldest→newest.
        If start_sprint is set, begin from that sprint; otherwise use the most recent count."""
        all_iters = self._get_all_iterations(team_id)

        def sort_key(s):
            attrs = s.get("attributes") or {}
            return attrs.get("startDate") or attrs.get("finishDate") or s.get("name", "")

        all_iters.sort(key=sort_key)
        print(f"  START_SPRINT={start_sprint!r}  total_iters={len(all_iters)}")

        matched = False
        if start_sprint:
            # Pass 1: match on sprint name or iteration path tail
            start_norm = _normalize(start_sprint.split("/")[-1])
            trimmed = []
            found = False
            for s in all_iters:
                name_norm = _normalize(s.get("name", ""))
                path_norm = _normalize((s.get("path") or "").split("\\")[-1])
                if not found and (start_norm in name_norm or start_norm in path_norm
                                  or name_norm in start_norm):
                    found = True
                if found:
                    trimmed.append(s)
            if trimmed:
                all_iters = trimmed
                matched = True
            else:
                # Pass 2: parse a date from the START_SPRINT value and filter by startDate
                # e.g. "2026/Q1/2026_S01_Dec31-Jan13" → startDate >= "2025-12-31"
                date_m = re.search(r"(\d{4})[-_]S\d+[-_](\w{3})(\d{2})", start_sprint)
                if date_m:
                    months = {"jan": "01", "feb": "02", "mar": "03", "apr": "04",
                              "may": "05", "jun": "06", "jul": "07", "aug": "08",
                              "sep": "09", "oct": "10", "nov": "11", "dec": "12"}
                    year = int(date_m.group(1))
                    mon = months.get(date_m.group(2).lower(), "01")
                    day = date_m.group(3)
                    # Dec/Nov in a sprint labeled with the following year starts in prior year
                    if mon in ("11", "12"):
                        year -= 1
                    cutoff = f"{year}-{mon}-{day}"
                    date_trimmed = [
                        s for s in all_iters
                        if (s.get("attributes") or {}).get("startDate", "9999") >= cutoff
                    ]
                    if date_trimmed:
                        print(f"  INFO: START_SPRINT matched by date cutoff {cutoff} "
                              f"({len(date_trimmed)} sprints).")
                        all_iters = date_trimmed
                        matched = True
                    else:
                        print(f"  WARNING: START_SPRINT '{start_sprint}' matched nothing "
                              f"by name or date — using most recent {count} sprints.")
                else:
                    print(f"  WARNING: START_SPRINT '{start_sprint}' matched nothing "
                          f"— using most recent {count} sprints.")

        # When no start anchor matched, take the most recent count sprints
        if not matched:
            all_iters = all_iters[-count:]

        # Use the last segment of the iteration path as the display name when it differs
        # from the generic "Sprint NNN" name ADO assigns internally
        for s in all_iters:
            path_tail = (s.get("path") or "").split("\\")[-1].strip()
            if path_tail and path_tail != s.get("name", ""):
                s["name"] = path_tail

        # Return oldest-first (up to count), so the chart reads left-to-right chronologically
        return all_iters[:count]

    def get_backlog_work_item_ids(self, team_name: str) -> list[int]:
        """Return IDs of User Stories in the team's backlog (not assigned to any sprint)."""
        wiql = (
            f"SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.project}' "
            f"AND [System.WorkItemType] IN ('User Story', 'Bug') "
            f"AND [System.State] NOT IN ('Removed', 'Done', 'Closed') "
            f"AND [System.IterationPath] = '{self.project}'"
        )
        url = f"{self.proj_base}/_apis/wit/wiql"
        try:
            resp = self.session.post(
                url,
                json={"query": wiql},
                params={"api-version": "7.1", "$top": 500},
                timeout=30,
            )
            if resp.status_code in (400, 404):
                return []
            resp.raise_for_status()
            data = self._parse_json(resp)
            return [item["id"] for item in data.get("workItems", [])]
        except (requests.exceptions.HTTPError, ValueError):
            return []

    def get_team_backlog_work_item_ids(self, team_id: str) -> list[int]:
        """Return IDs of User Stories in the team's backlog via the backlog API."""
        url = (
            f"{self.proj_base}/{team_id}/_apis/work/backlogs/"
            f"Microsoft.RequirementCategory/workItems"
        )
        try:
            resp = self.session.get(url, params={"api-version": "7.1"}, timeout=30)
            if resp.status_code in (400, 404):
                return []
            resp.raise_for_status()
            data = self._parse_json(resp)
            return [item["target"]["id"] for item in data.get("workItems", []) if item.get("target")]
        except (requests.exceptions.HTTPError, ValueError):
            return []

    def get_sprint_work_item_ids(self, team_id: str, iteration_id: str) -> list[int]:
        url = (
            f"{self.proj_base}/{team_id}/_apis/work/teamsettings/"
            f"iterations/{iteration_id}/workitems"
        )
        try:
            resp = self.session.get(url, params={"api-version": "7.1"}, timeout=30)
            if resp.status_code in (400, 404):
                return []
            resp.raise_for_status()
            data = self._parse_json(resp)
            return [
                item["target"]["id"]
                for item in data.get("workItemRelations", [])
                if item.get("target") and item["rel"] is None
            ]
        except requests.exceptions.HTTPError:
            return []

    def get_work_items(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        results = []
        # POST workitemsbatch silently drops relations; GET with $expand=relations works.
        # Omit the fields filter — combining fields + $expand causes a 400.
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            ids_param = ",".join(map(str, chunk))
            url = (
                f"{self.proj_base}/_apis/wit/workitems"
                f"?ids={ids_param}&api-version=7.1&%24expand=relations"
            )
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            results.extend(self._parse_json(resp).get("value", []))
        return results

    def get_work_item_updates(self, wi_id: int) -> list[dict]:
        """Return all field-update records for a work item (state-change history)."""
        url = f"{self.proj_base}/_apis/wit/workitems/{wi_id}/updates"
        data = self._get(url, params={"api-version": "7.1"})
        return data.get("value", [])

    def parse_relations(self, work_item: dict) -> dict:
        pr_urls = []
        has_commit = False
        parent_id = None
        for rel in work_item.get("relations") or []:
            rel_type = rel.get("rel", "")
            rel_name = (rel.get("attributes") or {}).get("name", "")
            url = rel.get("url", "")
            if rel_name == _GITHUB_PR_RELATION:
                pr_urls.append(url)
            elif rel_name == _GITHUB_COMMIT_RELATION:
                has_commit = True
            elif rel_type == "System.LinkTypes.Hierarchy-Reverse" and parent_id is None:
                # Extract numeric work item ID from the URL tail
                m = re.search(r"/(\d+)$", url)
                if m:
                    parent_id = int(m.group(1))
        return {"pr_urls": pr_urls, "has_commit": has_commit, "parent_id": parent_id}

    def get_work_item_fields(self, ids: list[int], fields: list[str]) -> dict:
        """Batch-fetch specific fields for a list of work item IDs.
        Returns {id: fields_dict}. Uses POST workitemsbatch (no $expand).
        """
        if not ids:
            return {}
        result = {}
        for start in range(0, len(ids), 200):
            chunk = ids[start: start + 200]
            url = f"{self.proj_base}/_apis/wit/workitemsbatch"
            payload = {"ids": chunk, "fields": fields}
            try:
                data = self._post(url, payload, params={"api-version": "7.1"})
                for item in data.get("value", []):
                    result[item["id"]] = item.get("fields", {})
            except Exception:
                pass
        return result

    def get_work_item_comments(self, wi_id: int) -> list[str]:
        """Return plain-text comment strings for a work item."""
        url = f"{self.proj_base}/_apis/wit/workitems/{wi_id}/comments"
        try:
            data = self._get(url, params={"api-version": "7.1-preview.3"})
            texts = []
            for c in data.get("comments", []):
                raw = c.get("text") or ""
                # Strip HTML tags
                plain = re.sub(r"<[^>]+>", " ", raw)
                plain = re.sub(r"\s+", " ", plain).strip()
                if plain:
                    texts.append(plain)
            return texts
        except Exception:
            return []

