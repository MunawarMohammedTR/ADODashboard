"""FastAPI server — replaces the hand-rolled http.server from main.py."""
import asyncio
import os
import threading
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()


def _make_clients():
    from ado_client import AzureDevOpsClient
    from github_client import GitHubClient

    ado = AzureDevOpsClient()
    gh = GitHubClient(ado_session=ado.session, ado_base=ado.base)
    return ado, gh


# ---------------------------------------------------------------------------
# Shared mutable state (same pattern as the original _Handler)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_state: dict = {
    "html": b"",
    "refreshing": False,
    "error": None,
    "team_panels": {},   # team_id → rendered HTML fragment
}
_ado = None
_gh = None


_SERVE_SPRINT_COUNT = int(os.environ.get("SERVE_SPRINT_COUNT", "3"))


def _regenerate() -> None:
    from main import build_report_data
    from report_generator import generate_report

    output_dir = os.environ.get("OUTPUT_DIR", ".")
    data = build_report_data(_ado, _gh, sprint_count_override=_SERVE_SPRINT_COUNT)
    _, html = generate_report(data, output_dir, serve_mode=True)
    with _lock:
        _state["html"] = html.encode("utf-8")


def _refresh_worker() -> None:
    try:
        _regenerate()
        with _lock:
            _state["refreshing"] = False
            _state["error"] = None
            _state["team_panels"] = {}   # invalidate panel cache after full refresh
    except Exception as exc:
        with _lock:
            _state["refreshing"] = False
            _state["error"] = str(exc)


# ---------------------------------------------------------------------------
# Lifespan: initialise clients immediately, kick off first fetch in background
# so the server starts accepting traffic right away and /status reports progress
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ado, _gh
    print("Initialising ADO + GitHub clients...")
    _ado, _gh = await asyncio.get_event_loop().run_in_executor(None, _make_clients)
    print("Server ready. Starting background data fetch...")
    with _lock:
        _state["refreshing"] = True
        _state["error"] = None
    threading.Thread(target=_refresh_worker, daemon=True).start()
    yield


app = FastAPI(title="ADO Dashboard", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
_LOADING_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>ADO Dashboard - Loading</title>
<meta http-equiv="refresh" content="5">
<style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f5f5f5}
.box{text-align:center;padding:2rem;background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1)}
h2{margin:0 0 .5rem}p{color:#666;margin:0}</style></head>
<body><div class="box"><h2>Loading dashboard...</h2>
<p>Fetching data from Azure DevOps. This page will refresh automatically.</p></div></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    with _lock:
        body = _state["html"]
        refreshing = _state["refreshing"]
    if not body:
        return HTMLResponse(content=_LOADING_PAGE, status_code=200)
    return HTMLResponse(content=body.decode("utf-8"))


@app.get("/status")
async def status():
    with _lock:
        return JSONResponse({"refreshing": _state["refreshing"], "error": _state["error"]})


@app.get("/teams")
async def teams():
    result = await asyncio.get_event_loop().run_in_executor(None, _ado.get_all_teams)
    return JSONResponse(result)


@app.get("/team-panel/{team_id}", response_class=HTMLResponse)
async def team_panel(team_id: str):
    from main import _process_team_json
    from report_generator import _render_team_panel_fragment

    # Serve from in-memory cache if available
    with _lock:
        cached = _state["team_panels"].get(team_id)
    if cached:
        return HTMLResponse(content=cached)

    all_teams = await asyncio.get_event_loop().run_in_executor(None, _ado.get_all_teams)
    matched = next((t for t in all_teams if t["id"] == team_id), None)
    if not matched:
        raise HTTPException(status_code=404, detail=f"Team '{team_id}' not found")

    result = await asyncio.get_event_loop().run_in_executor(
        None, _process_team_json, matched, _ado, _gh
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Team has no sprint configuration")

    html = _render_team_panel_fragment(result, serve_mode=True)
    with _lock:
        _state["team_panels"][team_id] = html
    return HTMLResponse(content=html)


@app.post("/refresh")
async def refresh(background_tasks: BackgroundTasks):
    with _lock:
        already = _state["refreshing"]
        if not already:
            _state["refreshing"] = True
            _state["error"] = None

    if already:
        return JSONResponse({"started": False, "reason": "already running"})

    background_tasks.add_task(
        asyncio.get_event_loop().run_in_executor, None, _refresh_worker
    )
    return JSONResponse({"started": True})
