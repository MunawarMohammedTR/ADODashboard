# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Generate static HTML report (batch mode)
python main.py

# Run FastAPI dev server with live refresh (recommended)
python main.py --serve
python main.py --serve --port 8080

# Run FastAPI server directly via uvicorn (with auto-reload for development)
uvicorn server:app --reload --port 8080

# Auto-generated API docs (available in serve mode)
# http://localhost:8080/docs

# Debug utilities
python debug_teams.py       # List all teams (helps configure TEAMS_FILTER)
python debug_relations.py   # Inspect work item relations and PR links
python debug_board.py       # Analyze board column transitions
python debug_updates.py     # Examine work item revision history
```

There are no automated tests. Verify changes by running the app in serve mode and checking the rendered output.

## Environment Setup

Copy `.env` and populate:

```
ADO_ORG          # Azure DevOps org URL or name
ADO_PROJECT      # ADO project URL or name
ADO_PAT          # PAT with Read access to Work Items + Code
GITHUB_TOKEN     # GitHub PAT for PR fetching

# Optional
GITHUB_REPO      # Comma-separated owner/repo for direct GitHub PR queries
SPRINT_COUNT     # Sprints to display (default: 3)
START_SPRINT     # Sprint name or date to start from
TEAMS_FILTER     # Comma-separated team names or board URLs
INCLUDE_BACKLOG  # true/false
TRACK_EFFORT     # true/false — parse board history for dev/QA day counts
FETCH_COMMENTS   # true/false — fetch work item comments for roadblock detection
OUTPUT_DIR       # Output directory for HTML report
PUBLISH_TO_AZURE # true/false
AZURE_STORAGE_CONNECTION_STRING
AZURE_STORAGE_CONTAINER  # default: "$web"
```

`github_login_map.json` maps GitHub login names to ADO display names for PR attribution.

## Architecture

### Data Flow

```
main.py
  └→ build_report_data()
      ├→ AzureDevOpsClient — fetches teams, sprints, work items, board history
      └→ _process_team() [per team]
          ├→ Pass 1: batch fetch work items per sprint
          ├→ Pass 2: fetch descriptions + resolve Feature parent titles
          └→ Pass 3: _audit_story() per story
              ├→ GitHubClient — PR details, review status, URL resolution
              ├→ _parse_effort() — dev/QA days from column history
              └→ _extract_roadblocks() — regex on description/comments
  └→ report_generator.generate_report()
      └→ Jinja2 render → HTML file → [optional] Azure Blob upload
```

### Key Modules

**`main.py`** — Entry point and orchestration. Two execution modes:
- *Batch*: fetch all data → render HTML → optional Azure publish → exit
- *Serve*: HTTP server with `/refresh`, `/team-panel/{id}`, `/teams`, `/status` endpoints; background thread rebuilds cache

**`ado_client.py`** — Azure DevOps REST API wrapper (PAT auth). Handles pagination, fuzzy team/sprint name matching, and batch work item fetches.

**`github_client.py`** — GitHub REST API client. Caches PR details and resolved URLs. Resolves `vstfs:///GitHub/*` ADO references to real GitHub URLs via service endpoint lookup. Implements exponential backoff for rate limits.

**`report_generator.py`** — Jinja2 rendering. Transforms raw data into JS-consumable structures for the dashboard's client-side filtering (`chart_data`, `story_data`, `feature_data`, `pr_by_individual`).

**`templates/`** — Two Jinja2 templates:
- `report.html.j2` — full dashboard (sprint charts, story list, feature rollup, PR View tab)
- `team_panel.html.j2` — team panel fragment for on-demand serve-mode loading

### Metrics Computed in `_sprint_summary()`

- **Health score** (0–100): completion rate (40 pts) + audit pass rate (40 pts) − roadblock penalty (up to 20 pts)
- **Audit pass rate**: 4 checks per story — story points, PR linked, commit linked, acceptance criteria present
- **Effort tracking**: dev/QA days inferred from board column transition timestamps
- **Roadblock detection**: keyword regex on description/comments + structural signals (WIP >5 days, sized story with no PR)

### Deployment

Azure Static Web Apps with AAD SSO auth. `setup_azure.ps1` provisions infrastructure. `staticwebapp.config.json` enforces authentication on all routes. GitHub Actions (`.github/workflows/publish.yml`) handles CI/CD on push.
