from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def _build_story_data(teams: list[dict]) -> dict:
    """Build compact per-team/sprint story data for the assignee filter JS."""
    out = {}
    for team in teams:
        team_stories = {}
        for sprint in team["sprints"]:
            team_stories[sprint["id"]] = {
                "name": sprint["name"],
                "isCurrent": not sprint.get("is_backlog", False),
                "stories": [
                    {
                        "assignedTo": s["assigned_to"],
                        "state": s["state"],
                        "points": s["story_points"] or 0,
                        "hasAc": s["has_ac"],
                        "hasPr": s["has_pr"],
                        "hasCommit": s["has_commit"],
                        "hasPoints": bool(s["story_points"]),
                    }
                    for s in sprint["data"]["stories"]
                ],
            }
        out[team["id"]] = team_stories
    return out


def generate_report(data: dict, output_dir: str = ".") -> str:
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
        }

    html = template.render(
        project=data["project"],
        generated_at=data["generated_at"],
        sprint_count=data["sprint_count"],
        start_sprint=data.get("start_sprint", ""),
        teams=data["teams"],
        chart_data=chart_data,
        story_data=_build_story_data(data["teams"]),
    )

    filename = f"ado_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    out_path = Path(output_dir) / filename
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)
