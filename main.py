import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv

from ado_client import AzureDevOpsClient
from github_client import GitHubClient
from report_generator import generate_report

load_dotenv()

_COMPLETED_STATES = {"Resolved", "Done", "Closed"}
_ADO_ORG_RE = re.compile(r"https?://dev\.azure\.com/([^/]+)")


def _ado_wi_url(base: str, project: str, work_item_id: int) -> str:
    return f"{base}/{project}/_workitems/edit/{work_item_id}"


def _assigned_to_display(raw: str | None) -> str:
    if not raw:
        return "Unassigned"
    m = re.match(r"^(.*?)\s*<", raw)
    return m.group(1).strip() if m else raw


def _audit_story(work_item: dict, ado: AzureDevOpsClient, gh: GitHubClient) -> dict:
    fields = work_item.get("fields", {})
    relations = ado.parse_relations(work_item)

    story_points = fields.get("Microsoft.VSTS.Scheduling.StoryPoints")
    ac_raw = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or ""
    has_ac = bool(ac_raw.strip())
    has_pr = len(relations["pr_urls"]) > 0
    has_commit = relations["has_commit"]
    pr_reviewed = any(gh.has_approved_review(url) for url in relations["pr_urls"])

    pr_html_url = None
    if relations["pr_urls"]:
        pr_html_url = gh.get_pr_html_url(relations["pr_urls"][0])

    wi_id = work_item["id"]
    return {
        "id": wi_id,
        "ado_url": _ado_wi_url(ado.base, ado.project, wi_id),
        "title": fields.get("System.Title", "(no title)"),
        "work_item_type": fields.get("System.WorkItemType", "User Story"),
        "state": fields.get("System.State", ""),
        "assigned_to": _assigned_to_display(fields.get("System.AssignedTo", {}).get("displayName") if isinstance(fields.get("System.AssignedTo"), dict) else fields.get("System.AssignedTo")),
        "story_points": story_points,
        "has_ac": has_ac,
        "has_pr": has_pr,
        "has_commit": has_commit,
        "pr_reviewed": pr_reviewed,
        "pr_url": pr_html_url,
    }


def _sprint_summary(stories: list[dict]) -> dict:
    total = len(stories)
    completed = sum(1 for s in stories if s["state"] in _COMPLETED_STATES)
    total_points = sum(s["story_points"] or 0 for s in stories)
    completed_points = sum(
        (s["story_points"] or 0) for s in stories if s["state"] in _COMPLETED_STATES
    )
    missing_points = sum(1 for s in stories if not s["story_points"])
    no_pr = sum(1 for s in stories if not s["has_pr"])
    no_commit = sum(1 for s in stories if not s["has_commit"])
    no_ac = sum(1 for s in stories if not s["has_ac"])
    no_review = sum(1 for s in stories if s["has_pr"] and not s["pr_reviewed"])

    audit_checks = 4
    total_checks = total * audit_checks
    passed_checks = (
        sum(1 for s in stories if s["has_ac"])
        + sum(1 for s in stories if s["has_pr"])
        + sum(1 for s in stories if s["has_commit"])
        + sum(1 for s in stories if s["story_points"])
    )
    audit_pass_rate = round(passed_checks / total_checks * 100) if total_checks else 100

    return {
        "total": total,
        "completed": completed,
        "total_points": round(total_points, 1),
        "completed_points": round(completed_points, 1),
        "missing_points": missing_points,
        "no_pr": no_pr,
        "no_commit": no_commit,
        "no_ac": no_ac,
        "no_review": no_review,
        "audit_pass_rate": audit_pass_rate,
        "stories": stories,
    }


def build_report_data(ado: AzureDevOpsClient, gh: GitHubClient) -> dict:
    sprint_count = int(os.environ.get("SPRINT_COUNT", "3"))
    start_sprint = os.environ.get("START_SPRINT", "").strip()
    include_backlog = os.environ.get("INCLUDE_BACKLOG", "false").lower() == "true"
    project = ado.project

    print(f"Fetching teams for project '{project}'...")
    teams_raw = ado.get_teams()
    if not teams_raw:
        print("No teams found. Check ADO_PROJECT and TEAMS_FILTER settings.")
        sys.exit(1)
    print(f"  Found {len(teams_raw)} team(s)")

    teams_out = []
    for team in teams_raw:
        print(f"\nTeam: {team['name']}")
        sprints = ado.get_sprints(team["id"], sprint_count, start_sprint)
        if not sprints:
            print("  (no sprint configuration — skipping)")
            continue
        print(f"  Sprints: {[s['name'] for s in sprints]}")

        sprint_out = []
        for sprint in sprints:
            print(f"  -> {sprint['name']} ...", end=" ", flush=True)
            ids = ado.get_sprint_work_item_ids(team["id"], sprint["id"])
            work_items = ado.get_work_items(ids)
            user_stories = [
                wi for wi in work_items
                if wi.get("fields", {}).get("System.WorkItemType") in ("User Story", "Bug")
            ]
            print(f"{len(user_stories)} work items")

            stories = [_audit_story(wi, ado, gh) for wi in user_stories]
            sprint_out.append({
                "id": sprint["id"],
                "name": sprint["name"],
                "start": (sprint.get("attributes") or {}).get("startDate", ""),
                "finish": (sprint.get("attributes") or {}).get("finishDate", ""),
                "is_backlog": False,
                "data": _sprint_summary(stories),
            })

        if include_backlog:
            print(f"  -> Backlog ...", end=" ", flush=True)
            backlog_ids = ado.get_team_backlog_work_item_ids(team["id"])
            sprint_item_ids = {s for sprint in sprint_out for s in []}
            backlog_work_items = ado.get_work_items(backlog_ids)
            backlog_stories = [
                wi for wi in backlog_work_items
                if wi.get("fields", {}).get("System.WorkItemType") in ("User Story", "Bug")
            ]
            print(f"{len(backlog_stories)} work items")
            if backlog_stories:
                stories = [_audit_story(wi, ado, gh) for wi in backlog_stories]
                sprint_out.insert(0, {
                    "id": "backlog",
                    "name": "Backlog",
                    "start": "",
                    "finish": "",
                    "is_backlog": True,
                    "data": _sprint_summary(stories),
                })

        teams_out.append({
            "id": team["id"],
            "name": team["name"],
            "sprints": sprint_out,
        })

    return {
        "project": project,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sprint_count": sprint_count,
        "start_sprint": start_sprint,
        "include_backlog": include_backlog,
        "teams": teams_out,
    }


def main():
    ado = AzureDevOpsClient()
    gh = GitHubClient(ado_session=ado.session, ado_base=ado.base)

    print("=" * 60)
    print("ADO Manager Dashboard Generator")
    print("=" * 60)

    data = build_report_data(ado, gh)

    output_dir = os.environ.get("OUTPUT_DIR", ".")
    out_path = generate_report(data, output_dir)
    print(f"\nReport written to: {out_path}")
    print("Open it in any browser.")


if __name__ == "__main__":
    main()
