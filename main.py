import concurrent.futures
import json
import os
import re
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from ado_client import AzureDevOpsClient
from github_client import GitHubClient
from report_generator import generate_report

load_dotenv()

_COMPLETED_STATES = {"Resolved", "Done", "Closed"}
_COL_DEV          = "In Development"   # board column name where dev work happens
_COL_QA           = "QA"               # board column name where QA happens
_track_effort     = os.environ.get("TRACK_EFFORT", "false").lower() == "true"
_fetch_comments   = os.environ.get("FETCH_COMMENTS", "false").lower() == "true"
_github_repos     = [r.strip() for r in os.environ.get("GITHUB_REPO", "tr/confirmation_api-adapter-be,tr/confirmation_nucleus-be,tr/confirmation_primary-record-service,tr/confirmation_api_proxy-be,tr/confirmation_file-base-auto-process-be,tr/confirmation_forms-be,tr/confirmation_self-registration-be,tr/confirmation_emails-be,tr/confirmation_libraries,tr/confirmation_authorizations-be,tr/confirmation_pricing-be,tr/confirmation_local-stack,tr/confirmation_web-components-fe,tr/confirmation_web-components-be,tr/confirmation_reports-be,tr/confirmation_iam-be,tr/confirmation_legacy-data-scripts-dbtr/confirmation_legacy-external-api,tr/confirmation_legacy-internal-api,tr/confirmation_legacy-libraries,tr/confirmation_legacy-schema-db,tr/confirmation_legacy-thirdparty-libs,tr/confirmation_legacy-web-apps-be").split(",") if r.strip()]
_ADO_ORG_RE = re.compile(r"https?://dev\.azure\.com/([^/]+)")

# GitHub login → ADO display name mapping (github_login_map.json)
_LOGIN_MAP_PATH = os.path.join(os.path.dirname(__file__), "github_login_map.json")
try:
    with open(_LOGIN_MAP_PATH, encoding="utf-8") as _f:
        _login_to_ado: dict[str, str] = {
            k: v for k, v in json.load(_f).items() if not k.startswith("_")
        }
    print(f"  [Config] Loaded {len(_login_to_ado)} GitHub login -> ADO name mapping(s)")
except FileNotFoundError:
    _login_to_ado = {}
except Exception as _e:
    print(f"  [Config] Warning: could not load github_login_map.json: {_e}")
    _login_to_ado = {}

# Cache GitHub PR fetches by (start, finish) window so multiple teams sharing
# the same sprint dates don't repeat identical REST calls.
_sprint_pr_cache: dict[tuple[str, str], list[dict]] = {}

_BLOCKER_RE = re.compile(
    r".{0,60}(?:blocked|blocker|blocking|impediment|waiting on|on hold|pending|dependency|risk|stuck).{0,60}",
    re.IGNORECASE,
)


def _ado_wi_url(base: str, project: str, work_item_id: int) -> str:
    return f"{base}/{project}/_workitems/edit/{work_item_id}"


def _assigned_to_display(raw: str | None) -> str:
    if not raw:
        return "Unassigned"
    m = re.match(r"^(.*?)\s*<", raw)
    result = m.group(1) if m else raw
    return " ".join(result.split())


def _assigned_to_name(raw) -> str:
    if isinstance(raw, dict):
        raw = raw.get("displayName")
    return _assigned_to_display(raw)


def _parse_effort(updates: list[dict]) -> dict:
    """Derive dev_days / qa_days from board column changes in work item history.

    Tracks System.BoardColumn transitions:
      dev_start  — card enters _COL_DEV ("In Development")
      dev_end    — card enters _COL_QA  ("QA")   [includes "Dev Ready-Pending Deployment" wait]
      qa_start   — card enters _COL_QA
      qa_end     — card leaves _COL_QA

    9999 sentinel dates are skipped.
    """
    dev_start = dev_end = qa_start = qa_end = None

    for rev in updates:
        col_change = (rev.get("fields") or {}).get("System.BoardColumn")
        if not col_change:
            continue
        old_col = col_change.get("oldValue") or ""
        new_col = col_change.get("newValue") or ""
        ts_raw = rev.get("revisedDate", "")
        if not ts_raw or ts_raw.startswith("9999"):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue

        if new_col == _COL_DEV and dev_start is None:
            dev_start = ts
        if new_col == _COL_QA and dev_start is not None and dev_end is None:
            dev_end = ts
        if new_col == _COL_QA and qa_start is None:
            qa_start = ts
        if old_col == _COL_QA and qa_start is not None and qa_end is None:
            qa_end = ts

    now = datetime.now(timezone.utc)

    def _elapsed(start, end):
        if start is None:
            return None, False
        finish = end if end is not None else now
        return round((finish - start).total_seconds() / 86400, 1), end is None

    dev_days, dev_wip = _elapsed(dev_start, dev_end)
    qa_days,  qa_wip  = _elapsed(qa_start,  qa_end)
    return {"dev_days": dev_days, "dev_wip": dev_wip, "qa_days": qa_days, "qa_wip": qa_wip}


def _extract_feature_from_text(text: str) -> str | None:
    """Return a feature name from [Feature: X] or 'Feature: X' patterns, or None."""
    if not text:
        return None
    m = re.search(r"\[Feature:\s*([^\]]+)\]", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"Feature[:\s]+([^\n;|<]{3,60})", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _extract_roadblocks(texts: list[str], wi_id: int, title: str, ado_url: str) -> list[dict]:
    """Return up to 5 unique blocker dicts {text, id, title, ado_url} from texts."""
    seen: set[str] = set()
    results: list[dict] = []
    for text in texts:
        for m in _BLOCKER_RE.finditer(text):
            phrase = m.group(0).strip()
            key = phrase.lower()
            if key not in seen:
                seen.add(key)
                results.append({"text": phrase, "id": wi_id, "title": title, "ado_url": ado_url})
                if len(results) >= 5:
                    return results
    return results


def _audit_story(work_item: dict, ado: AzureDevOpsClient, gh: GitHubClient,
                 description: str = "", comments: list[str] | None = None,
                 feature_map: dict | None = None) -> dict:
    fields = work_item.get("fields", {})
    relations = ado.parse_relations(work_item)

    story_points = fields.get("Microsoft.VSTS.Scheduling.StoryPoints")
    tags_raw = fields.get("System.Tags") or ""
    tags = [t.strip() for t in tags_raw.split(";") if t.strip()]
    ac_raw = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or ""
    has_ac = bool(ac_raw.strip())
    has_pr = len(relations["pr_urls"]) > 0
    has_commit = relations["has_commit"]
    pr_reviewed = any(gh.has_approved_review(url) for url in relations["pr_urls"])

    pr_html_url = None
    pr_details: list[dict] = []
    for raw_url in relations["pr_urls"]:
        details = gh.get_pr_details(raw_url)
        if details:
            pr_details.append(details)
    if pr_details and not pr_html_url:
        pr_html_url = pr_details[0]["html_url"]
    elif relations["pr_urls"] and not pr_html_url:
        pr_html_url = gh.get_pr_html_url(relations["pr_urls"][0])

    wi_id = work_item["id"]
    effort = _parse_effort(ado.get_work_item_updates(wi_id)) if _track_effort else \
             {"dev_days": None, "dev_wip": False, "qa_days": None, "qa_wip": False}

    # Feature attribution: parent link → description pattern → "Unassigned"
    feature_name = "Unassigned"
    parent_id = relations.get("parent_id")
    if parent_id and feature_map and parent_id in feature_map:
        feature_name = feature_map[parent_id]
    else:
        from_text = _extract_feature_from_text(description)
        if from_text:
            feature_name = from_text

    wi_title = fields.get("System.Title", "(no title)")
    wi_url   = _ado_wi_url(ado.base, ado.project, wi_id)

    # Roadblock detection from description + comments
    all_texts = [description] + (comments or [])
    roadblocks = _extract_roadblocks(all_texts, wi_id, wi_title, wi_url)

    # Structural roadblock signals
    state = fields.get("System.State", "")
    if state not in _COMPLETED_STATES:
        dev_days = effort["dev_days"]
        if effort.get("dev_wip") and dev_days is not None and dev_days > 5:
            roadblocks.append({"text": f"In development for {dev_days} days (WIP)",
                                "id": wi_id, "title": wi_title, "ado_url": wi_url})
        if not has_pr and (story_points or 0) >= 3:
            roadblocks.append({"text": "No PR linked for a sized item",
                                "id": wi_id, "title": wi_title, "ado_url": wi_url})

    return {
        "id": wi_id,
        "ado_url": _ado_wi_url(ado.base, ado.project, wi_id),
        "title": fields.get("System.Title", "(no title)"),
        "work_item_type": fields.get("System.WorkItemType", "User Story"),
        "tags": tags,
        "state": state,
        "assigned_to": _assigned_to_name(fields.get("System.AssignedTo")),
        "story_points": story_points,
        "has_ac": has_ac,
        "has_pr": has_pr,
        "has_commit": has_commit,
        "pr_reviewed": pr_reviewed,
        "pr_url": pr_html_url,
        "pr_details": pr_details,
        "dev_days": effort["dev_days"],
        "dev_wip":  effort["dev_wip"],
        "qa_days":  effort["qa_days"],
        "qa_wip":   effort["qa_wip"],
        "feature":  feature_name,
        "roadblocks": roadblocks,
    }


def _pr_by_individual_from_list(prs: list[dict], login_to_ado: dict[str, str] | None = None) -> dict[str, dict]:
    """Aggregate a flat list of PR dicts (from GitHub direct query) into per-author stats.

    login_to_ado maps GitHub login → ADO display name, built from sprint stories.
    Each entry gets an 'assigned_to' field for filtering in the PR View.
    """
    result: dict[str, dict] = {}
    seen: set[str] = set()
    for pr in prs:
        url = pr.get("html_url") or ""
        if url in seen:
            continue
        seen.add(url)
        author = pr.get("author_login") or "Unknown"
        if author not in result:
            ado_name = (login_to_ado or {}).get(author, "")
            result[author] = {
                "prs_raised": 0, "merged_to_master": 0, "not_merged": 0,
                "pct_deployed": 0.0, "ai_labeled": 0, "ai_labeled_prs": [],
                "assigned_to": ado_name,
            }
        rec = result[author]
        rec["prs_raised"] += 1
        if pr.get("merged_to_master"):
            rec["merged_to_master"] += 1
        else:
            rec["not_merged"] += 1
        if pr.get("ai_labeled"):
            rec["ai_labeled"] += 1
            repo = "/".join((pr.get("html_url") or "").rstrip("/").split("/")[-4:-2]) if pr.get("html_url") else ""
            rec["ai_labeled_prs"].append({
                "repo": repo,
                "number": pr.get("number"),
                "html_url": pr.get("html_url", ""),
            })
    for rec in result.values():
        total = rec["prs_raised"]
        rec["pct_deployed"] = round(rec["merged_to_master"] / total * 100, 1) if total else 0.0
    return result


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

    dev_samples  = [s["dev_days"] for s in stories if s.get("dev_days") is not None]
    qa_samples   = [s["qa_days"]  for s in stories if s.get("qa_days")  is not None]
    avg_dev_days = round(sum(dev_samples) / len(dev_samples), 1) if dev_samples else None
    avg_qa_days  = round(sum(qa_samples)  / len(qa_samples),  1) if qa_samples  else None

    # Health score: completion (40pts) + audit pass rate (40pts) - roadblock penalty (up to 20pts)
    completion_rate = (completed / total) if total else 1.0
    total_roadblocks = sum(len(s.get("roadblocks", [])) for s in stories)
    roadblock_penalty = min(total_roadblocks * 10, 20)
    health_score = round(completion_rate * 40 + audit_pass_rate / 100 * 40 + (20 - roadblock_penalty))
    health_score = max(0, min(100, health_score))
    health_label = "green" if health_score >= 75 else ("amber" if health_score >= 45 else "red")

    # PR stats per individual — keyed by GitHub author login (falls back to ADO assignee)
    pr_by_individual: dict[str, dict] = {}
    seen_prs: set[str] = set()
    for s in stories:
        pr_details = s.get("pr_details") or []
        pr_urls = [p.get("html_url") or "" for p in pr_details]

        # Fallback: if no resolved PR details but work item has a PR URL, count via ADO assignee
        if not pr_details and s.get("has_pr"):
            key = f"_raw_{s['id']}"
            if key not in seen_prs:
                seen_prs.add(key)
                author = s.get("assigned_to") or "Unknown"
                if author not in pr_by_individual:
                    pr_by_individual[author] = {"prs_raised": 0, "merged_to_master": 0, "not_merged": 0, "pct_deployed": 0.0, "ai_labeled": 0, "ai_labeled_prs": [], "assigned_to": author}
                pr_by_individual[author]["prs_raised"] += 1
                pr_by_individual[author]["not_merged"] += 1
            continue

        for pr in pr_details:
            url = pr.get("html_url") or ""
            if url in seen_prs:
                continue
            seen_prs.add(url)
            author = pr.get("author_login") or s.get("assigned_to") or "Unknown"
            if author not in pr_by_individual:
                ado_name = _login_to_ado.get(author) or s.get("assigned_to") or ""
                pr_by_individual[author] = {"prs_raised": 0, "merged_to_master": 0, "not_merged": 0, "pct_deployed": 0.0, "ai_labeled": 0, "ai_labeled_prs": [], "assigned_to": ado_name}
            rec = pr_by_individual[author]
            rec["prs_raised"] += 1
            if pr.get("merged_to_master"):
                rec["merged_to_master"] += 1
            else:
                rec["not_merged"] += 1
            if pr.get("ai_labeled"):
                rec["ai_labeled"] += 1
                repo = "/".join(url.rstrip("/").split("/")[-4:-2]) if url else ""
                rec["ai_labeled_prs"].append({
                    "repo": repo,
                    "number": pr.get("number"),
                    "html_url": url,
                })
    for rec in pr_by_individual.values():
        total_raised = rec["prs_raised"]
        rec["pct_deployed"] = round(rec["merged_to_master"] / total_raised * 100, 1) if total_raised else 0.0

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
        "avg_dev_days": avg_dev_days,
        "avg_qa_days":  avg_qa_days,
        "health_score": health_score,
        "health_label": health_label,
        "pr_by_individual": pr_by_individual,
        "stories": stories,
    }


def _build_feature_rollup(sprints: list[dict]) -> list[dict]:
    """Aggregate all sprint stories by feature name into a feature-level summary."""
    feature_index: dict[str, dict] = {}

    for sprint in sprints:
        if sprint.get("is_backlog"):
            continue
        sprint_name = sprint["name"]
        for story in sprint["data"]["stories"]:
            fname = story.get("feature") or "Unassigned"
            if fname not in feature_index:
                feature_index[fname] = {
                    "name": fname,
                    "sprint_trend": [],
                    "total_items": 0,
                    "completed_items": 0,
                    "total_points": 0.0,
                    "done_points": 0.0,
                    "roadblocks": [],
                    "stories": [],
                    "_sprints_seen": {},
                }
            fe = feature_index[fname]
            fe["stories"].append(dict(story, sprint_name=sprint_name))
            fe["total_items"] += 1
            pts = story.get("story_points") or 0
            fe["total_points"] += pts
            done = story["state"] in _COMPLETED_STATES
            if done:
                fe["completed_items"] += 1
                fe["done_points"] += pts

            # Per-sprint trend bucket
            if sprint_name not in fe["_sprints_seen"]:
                fe["_sprints_seen"][sprint_name] = {
                    "sprint": sprint_name,
                    "total": 0, "completed": 0,
                    "points": 0.0, "done_points": 0.0,
                }
            bucket = fe["_sprints_seen"][sprint_name]
            bucket["total"] += 1
            bucket["points"] += pts
            if done:
                bucket["completed"] += 1
                bucket["done_points"] += pts

            # Collect roadblocks (deduplicated by text+id pair)
            existing_keys = {(r["text"], r["id"]) for r in fe["roadblocks"]}
            for rb in story.get("roadblocks", []):
                key = (rb["text"], rb["id"])
                if key not in existing_keys:
                    existing_keys.add(key)
                    fe["roadblocks"].append(rb)

    # Finalise each feature
    result = []
    for fe in feature_index.values():
        fe["sprint_trend"] = sorted(fe["_sprints_seen"].values(), key=lambda b: b["sprint"])
        del fe["_sprints_seen"]
        total = fe["total_items"]
        fe["completion_pct"] = round(fe["completed_items"] / total * 100) if total else 0
        fe["total_points"]   = round(fe["total_points"], 1)
        fe["done_points"]    = round(fe["done_points"], 1)
        result.append(fe)

    # Sort: most items first, Unassigned last
    result.sort(key=lambda f: (f["name"] == "Unassigned", -f["total_items"]))
    return result


def _process_team(
    team: dict,
    ado: AzureDevOpsClient,
    gh: GitHubClient,
    sprint_count: int,
    start_sprint: str,
    include_backlog: bool,
) -> dict | None:
    print(f"\nTeam: {team['name']}")
    sprints = ado.get_sprints(team["id"], sprint_count, start_sprint)
    if not sprints:
        print("  (no sprint configuration — skipping)")
        return None
    print(f"  Sprints: {[s['name'] for s in sprints]}")

    # ── Pass 1: collect all work items across sprints ─────────────────────
    sprint_raw: list[tuple[dict, list[dict]]] = []  # (sprint_meta, work_items)
    all_wi: dict[int, dict] = {}  # id → work_item (deduped)

    for sprint in sprints:
        print(f"  -> {sprint['name']} ...", end=" ", flush=True)
        ids = ado.get_sprint_work_item_ids(team["id"], sprint["id"])
        work_items = ado.get_work_items(ids)
        filtered = [
            wi for wi in work_items
            if wi.get("fields", {}).get("System.WorkItemType") in ("User Story", "Bug", "Task")
        ]
        print(f"{len(filtered)} work items")
        sprint_raw.append((sprint, filtered))
        for wi in filtered:
            all_wi[wi["id"]] = wi

    # ── Pass 2: batch-fetch descriptions + resolve parent Feature titles ──
    all_ids = list(all_wi.keys())
    print(f"  Fetching descriptions for {len(all_ids)} items...", end=" ", flush=True)
    desc_map = ado.get_work_item_fields(all_ids, ["System.Id", "System.Description"])
    print("done")

    parent_ids: set[int] = set()
    for wi in all_wi.values():
        rel = ado.parse_relations(wi)
        if rel.get("parent_id"):
            parent_ids.add(rel["parent_id"])

    feature_map: dict[int, str] = {}
    if parent_ids:
        print(f"  Resolving {len(parent_ids)} parent Feature titles...", end=" ", flush=True)
        parent_fields = ado.get_work_item_fields(
            list(parent_ids),
            ["System.Id", "System.Title", "System.WorkItemType", "System.Parent"],
        )
        epic_ids: dict[int, int] = {}  # epic_id -> story's parent_id (the epic itself)
        for pid, flds in parent_fields.items():
            wtype = flds.get("System.WorkItemType")
            if wtype == "Feature":
                feature_map[pid] = flds.get("System.Title") or f"Feature #{pid}"
            elif wtype == "Epic":
                grandparent = flds.get("System.Parent")
                if grandparent:
                    epic_ids[int(grandparent)] = pid

        # Second pass: stories whose direct parent is an Epic — look up the Epic's parent Feature
        if epic_ids:
            grandparent_fields = ado.get_work_item_fields(
                list(epic_ids.keys()),
                ["System.Id", "System.Title", "System.WorkItemType"],
            )
            for gpid, flds in grandparent_fields.items():
                if flds.get("System.WorkItemType") == "Feature":
                    epic_id = epic_ids[gpid]
                    feature_map[epic_id] = flds.get("System.Title") or f"Feature #{gpid}"

        print(f"{len(feature_map)} features found")

    comments_map: dict[int, list[str]] = {}
    if _fetch_comments:
        print("  Fetching comments...", end=" ", flush=True)
        for wi_id in all_ids:
            comments_map[wi_id] = ado.get_work_item_comments(wi_id)
        print("done")

    # ── Pass 3: audit stories with enriched data ──────────────────────────
    sprint_out = []
    for sprint, filtered in sprint_raw:
        stories = [
            _audit_story(
                wi, ado, gh,
                description=re.sub(r"<[^>]+>", " ",
                                   (desc_map.get(wi["id"]) or {}).get("System.Description") or ""),
                comments=comments_map.get(wi["id"], []),
                feature_map=feature_map,
            )
            for wi in filtered
        ]
        sprint_start  = (sprint.get("attributes") or {}).get("startDate", "")
        sprint_finish = (sprint.get("attributes") or {}).get("finishDate", "")
        summary = _sprint_summary(stories)

        # Override pr_by_individual with a direct GitHub repo query if configured
        if _github_repos and sprint_start and sprint_finish:
            cache_key = (sprint_start[:10], sprint_finish[:10])
            if cache_key in _sprint_pr_cache:
                all_gh_prs = _sprint_pr_cache[cache_key]
                print(f"  [GitHub] Sprint {cache_key[0]}/{cache_key[1]}: cache hit ({len(all_gh_prs)} PR(s))")
            else:
                all_gh_prs = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    future_to_repo = {
                        executor.submit(gh.get_prs_for_sprint, repo, sprint_start, sprint_finish): repo
                        for repo in _github_repos
                    }
                    for future in concurrent.futures.as_completed(future_to_repo):
                        repo = future_to_repo[future]
                        try:
                            repo_prs = future.result()
                        except Exception as exc:
                            print(f"  [GitHub] {repo} fetch failed: {exc}")
                            repo_prs = []
                        ai_count = sum(1 for p in repo_prs if p.get("ai_labeled"))
                        print(f"  [GitHub] {repo}: {len(repo_prs)} PR(s), {ai_count} AI-labeled")
                        all_gh_prs.extend(repo_prs)
                _sprint_pr_cache[cache_key] = all_gh_prs
            if all_gh_prs:
                summary["pr_by_individual"] = _pr_by_individual_from_list(all_gh_prs, _login_to_ado)

        sprint_out.append({
            "id": sprint["id"],
            "name": sprint["name"],
            "start": sprint_start,
            "finish": sprint_finish,
            "is_backlog": False,
            "data": summary,
        })

    if include_backlog:
        print("  -> Backlog ...", end=" ", flush=True)
        backlog_ids = ado.get_team_backlog_work_item_ids(team["id"])
        backlog_work_items = ado.get_work_items(backlog_ids)
        backlog_stories_wi = [
            wi for wi in backlog_work_items
            if wi.get("fields", {}).get("System.WorkItemType") in ("User Story", "Bug", "Task")
        ]
        print(f"{len(backlog_stories_wi)} work items")
        if backlog_stories_wi:
            bl_ids = [wi["id"] for wi in backlog_stories_wi]
            bl_desc = ado.get_work_item_fields(bl_ids, ["System.Id", "System.Description"])
            stories = [
                _audit_story(
                    wi, ado, gh,
                    description=re.sub(r"<[^>]+>", " ",
                                       (bl_desc.get(wi["id"]) or {}).get("System.Description") or ""),
                    feature_map=feature_map,
                )
                for wi in backlog_stories_wi
            ]
            sprint_out.insert(0, {
                "id": "backlog",
                "name": "Backlog",
                "start": "",
                "finish": "",
                "is_backlog": True,
                "data": _sprint_summary(stories),
            })

    return {
        "id": team["id"],
        "name": team["name"],
        "sprints": sprint_out,
        "features": _build_feature_rollup(sprint_out),
    }


def _process_team_json(team: dict, ado: "AzureDevOpsClient", gh: "GitHubClient") -> "dict | None":
    sprint_count = int(os.environ.get("SPRINT_COUNT", "3"))
    start_sprint = os.environ.get("START_SPRINT", "").strip()
    include_backlog = os.environ.get("INCLUDE_BACKLOG", "false").lower() == "true"
    return _process_team(team, ado, gh, sprint_count, start_sprint, include_backlog)


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

    teams_out = [
        result
        for team in teams_raw
        for result in [_process_team(team, ado, gh, sprint_count, start_sprint, include_backlog)]
        if result is not None
    ]

    return {
        "project": project,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "sprint_count": sprint_count,
        "start_sprint": start_sprint,
        "include_backlog": include_backlog,
        "teams": teams_out,
    }


def _run_serve_mode(ado: "AzureDevOpsClient", gh: "GitHubClient", port: int = 8080) -> None:
    import http.server
    import json as _json
    import threading
    import webbrowser

    output_dir = os.environ.get("OUTPUT_DIR", ".")

    # Shared state protected by a lock
    _lock = threading.Lock()
    _state: dict = {
        "html": b"",
        "refreshing": False,  # True while background fetch is running
        "error": None,        # last error message, or None
    }

    def _regenerate() -> None:
        data = build_report_data(ado, gh)
        _, html = generate_report(data, output_dir, serve_mode=True)
        with _lock:
            _state["html"] = html.encode("utf-8")

    def _refresh_worker() -> None:
        try:
            _regenerate()
            with _lock:
                _state["refreshing"] = False
                _state["error"] = None
        except Exception as exc:
            with _lock:
                _state["refreshing"] = False
                _state["error"] = str(exc)

    def _json_response(handler: "http.server.BaseHTTPRequestHandler", status: int, payload: dict) -> None:
        resp = _json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(resp)))
        handler.end_headers()
        handler.wfile.write(resp)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                with _lock:
                    body = _state["html"]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/status":
                with _lock:
                    payload = {
                        "refreshing": _state["refreshing"],
                        "error": _state["error"],
                    }
                _json_response(self, 200, payload)
            elif self.path == "/teams":
                _json_response(self, 200, ado.get_all_teams())
            elif self.path.startswith("/team-panel/"):
                from report_generator import _render_team_panel_fragment
                team_id = self.path[len("/team-panel/"):]
                all_teams = ado.get_all_teams()
                matched = next((t for t in all_teams if t["id"] == team_id), None)
                if not matched:
                    self.send_response(404)
                    self.end_headers()
                    return
                result = _process_team_json(matched, ado, gh)
                if result is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = _render_team_panel_fragment(result, serve_mode=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path == "/refresh":
                with _lock:
                    already = _state["refreshing"]
                    if not already:
                        _state["refreshing"] = True
                        _state["error"] = None
                if already:
                    _json_response(self, 200, {"started": False, "reason": "already running"})
                else:
                    threading.Thread(target=_refresh_worker, daemon=True).start()
                    _json_response(self, 200, {"started": True})
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args) -> None:  # noqa: ANN002
            pass  # suppress default per-request stdout noise

    print("Fetching initial data...")
    _regenerate()

    server = http.server.HTTPServer(("localhost", port), _Handler)
    url = f"http://localhost:{port}/"
    print(f"\nDashboard ready at {url}  (Ctrl+C to stop)\n")
    # Open browser in a background thread so the server starts first
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


def main() -> None:
    args = sys.argv[1:]
    serve_mode = "--serve" in args
    port = 8080
    if "--port" in args:
        idx = args.index("--port")
        try:
            port = int(args[idx + 1])
        except (IndexError, ValueError):
            print("--port requires an integer value; defaulting to 8080.")

    ado = AzureDevOpsClient()
    gh = GitHubClient(ado_session=ado.session, ado_base=ado.base)

    print("=" * 60)
    print("ADO Manager Dashboard Generator")
    print("=" * 60)

    if serve_mode:
        _run_serve_mode(ado, gh, port=port)
        return

    data = build_report_data(ado, gh)
    output_dir = os.environ.get("OUTPUT_DIR", ".")
    out_path, _ = generate_report(data, output_dir)
    print(f"\nReport written to: {out_path}")
    print("Open it in any browser.")

    if os.environ.get("PUBLISH_TO_AZURE", "false").lower() == "true":
        from azure_publisher import publish_report
        url = publish_report(out_path)
        if url:
            print(f"Dashboard URL:     {url}")


if __name__ == "__main__":
    main()
