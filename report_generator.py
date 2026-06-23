from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def _build_feature_js_data(teams: list[dict]) -> dict:
    """Build compact per-team feature data for the Feature View JS."""
    out = {}
    for team in teams:
        features_js = []
        for fe in team.get("features", []):
            features_js.append({
                "name": fe["name"],
                "totalItems": fe["total_items"],
                "completedItems": fe["completed_items"],
                "totalPoints": fe["total_points"],
                "donePoints": fe["done_points"],
                "completionPct": fe["completion_pct"],
                "roadblocks": fe["roadblocks"],
                "sprintTrend": fe["sprint_trend"],
                "stories": [
                    {
                        "id": s["id"],
                        "title": s["title"],
                        "adoUrl": s["ado_url"],
                        "witType": s["work_item_type"],
                        "state": s["state"],
                        "assignedTo": s["assigned_to"],
                        "points": s["story_points"] or 0,
                        "sprintName": s.get("sprint_name", ""),
                    }
                    for s in fe["stories"]
                ],
            })
        out[team["id"]] = features_js
    return out


def _build_story_data(teams: list[dict]) -> dict:
    """Build compact per-team/sprint story data for the assignee filter JS."""
    out = {}
    for team in teams:
        team_stories = {}
        for sprint in team["sprints"]:
            team_stories[sprint["id"]] = {
                "name": sprint["name"],
                "isCurrent": not sprint.get("is_backlog", False),
                "healthScore": sprint["data"]["health_score"],
                "healthLabel": sprint["data"]["health_label"],
                "prByIndividual": sprint["data"].get("pr_by_individual", {}),
                "stories": [
                    {
                        "id": s["id"],
                        "title": s["title"],
                        "adoUrl": s["ado_url"],
                        "witType": s["work_item_type"],
                        "assignedTo": s["assigned_to"],
                        "state": s["state"],
                        "points": s["story_points"] or 0,
                        "hasAc": s["has_ac"],
                        "hasPr": s["has_pr"],
                        "hasCommit": s["has_commit"],
                        "hasPoints": bool(s["story_points"]),
                        "tags": s.get("tags", []),
                        "devDays": s.get("dev_days"),
                        "devWip":  s.get("dev_wip", False),
                        "qaDays":  s.get("qa_days"),
                        "qaWip":   s.get("qa_wip", False),
                        "roadblockCount": len(s.get("roadblocks", [])),
                    }
                    for s in sprint["data"]["stories"]
                ],
            }
        out[team["id"]] = team_stories
    return out


def _render_team_panel_fragment(team_data: dict, serve_mode: bool = False) -> str:
    """Render a single team panel HTML fragment for on-demand injection in serve mode."""
    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template("team_panel.html.j2")

    sprints_chron = [s for s in team_data["sprints"] if not s.get("is_backlog")]
    chart_data = {team_data["id"]: {
        "labels": [s["name"] for s in sprints_chron],
        "total": [s["data"]["total"] for s in sprints_chron],
        "completed": [s["data"]["completed"] for s in sprints_chron],
        "points_total": [s["data"]["total_points"] or 0 for s in sprints_chron],
        "points_done": [s["data"]["completed_points"] or 0 for s in sprints_chron],
    }}

    return template.render(
        team=team_data,
        serve_mode=serve_mode,
        story_data=_build_story_data([team_data]),
        feature_data=_build_feature_js_data([team_data]),
        chart_data=chart_data,
    )


def generate_report(data: dict, output_dir: str = ".", serve_mode: bool = False) -> tuple[str, str]:
    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template("report.html.j2")

    chart_data = {}
    for team in data["teams"]:
        sprints_chron = [s for s in team["sprints"] if not s.get("is_backlog")]
        chart_data[team["id"]] = {
            "labels": [s["name"] for s in sprints_chron],
            "total": [s["data"]["total"] for s in sprints_chron],
            "completed": [s["data"]["completed"] for s in sprints_chron],
            "points_total": [s["data"]["total_points"] or 0 for s in sprints_chron],
            "points_done": [s["data"]["completed_points"] or 0 for s in sprints_chron],
        }

    html = template.render(
        project=data["project"],
        generated_at=data["generated_at"],
        sprint_count=data["sprint_count"],
        start_sprint=data.get("start_sprint", ""),
        teams=data["teams"],
        chart_data=chart_data,
        story_data=_build_story_data(data["teams"]),
        feature_data=_build_feature_js_data(data["teams"]),
        serve_mode=serve_mode,
    )

    filename = f"ado_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    out_path = Path(output_dir) / filename
    out_path.write_text(html, encoding="utf-8")
    return str(out_path), html
