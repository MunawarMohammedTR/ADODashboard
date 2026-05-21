import os
from dotenv import load_dotenv
from ado_client import AzureDevOpsClient, _normalize, _extract_team_name

load_dotenv()

ado = AzureDevOpsClient()

url = f"{ado.base}/_apis/projects/{ado.project}/teams"
all_teams = []
top = 200
skip = 0
while True:
    resp = ado.session.get(url, params={"api-version": "7.1", "$top": top, "$skip": skip}, timeout=30)
    page = resp.json().get("value", [])
    all_teams.extend(page)
    if len(page) < top:
        break
    skip += top

print(f"Total teams: {len(all_teams)}")

keywords = ["confirm", "gryff", "asset", "assetver"]
matching = [t["name"] for t in all_teams if any(k in t["name"].lower() for k in keywords)]
print(f"\nConfirmation/Gryffindor/Asset teams ({len(matching)}):")
for n in matching:
    print(f"  {n!r}")

print("\nAll team names (sorted):")
for t in sorted(all_teams, key=lambda x: x["name"].lower()):
    print(f"  {t['name']}")
