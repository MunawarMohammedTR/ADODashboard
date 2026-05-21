"""
Fetch relations via single-item GET API on past sprint items.
"""
from dotenv import load_dotenv
from ado_client import AzureDevOpsClient

load_dotenv()
ado = AzureDevOpsClient()

teams = ado.get_teams()
team = teams[0]
sprints = ado.get_sprints(team["id"], 15, "2026/Q1/2026_S01_Dec31-Jan13")

# Find a past sprint with items (sprints are oldest-first; skip future ones)
from datetime import datetime, timezone
now = datetime.now(timezone.utc).isoformat()

sample_ids = []
chosen_sprint = None
for sprint in sprints:
    attrs = sprint.get("attributes") or {}
    finish = attrs.get("finishDate", "")
    if finish and finish < now:  # past sprint
        ids = ado.get_sprint_work_item_ids(team["id"], sprint["id"])
        if ids:
            sample_ids = ids[:5]
            chosen_sprint = sprint["name"]
            break

if not sample_ids:
    print("No past sprint items found — using S08 by name")
    for sprint in sprints:
        if "S08" in sprint["name"] or "S09" in sprint["name"] or "S10" in sprint["name"]:
            ids = ado.get_sprint_work_item_ids(team["id"], sprint["id"])
            if ids:
                sample_ids = ids[:5]
                chosen_sprint = sprint["name"]
                break

print(f"Sprint: {chosen_sprint}, checking items: {sample_ids}\n")

# Try single-item GET with $expand=relations (URL built manually)
for wi_id in sample_ids:
    url = f"{ado.proj_base}/_apis/wit/workitems/{wi_id}?api-version=7.1&%24expand=relations"
    resp = ado.session.get(url, timeout=30)
    wi = resp.json()
    title = wi.get("fields", {}).get("System.Title", "")[:55]
    relations = wi.get("relations") or []
    print(f"WI #{wi_id} — {title}")
    if not relations:
        print("  (no relations)")
    for rel in relations:
        name = (rel.get("attributes") or {}).get("name", "—")
        print(f"  rel={rel.get('rel')!r}  name={name!r}  url={rel.get('url','')[:80]}")
    print()
