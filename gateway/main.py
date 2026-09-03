"""Gateway web service for the Parallel research agent.

A lightweight FastAPI app that:
  POST /research        — accepts a query, dispatches a workflow run, returns the run ID
  GET  /research/{id}   — polls run status and returns the result when complete
  GET  /health          — liveness check for Render
  GET  /                — serves a minimal demo form
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from render import Render
from render.client import ListTaskRunsParams

app = FastAPI(
    title="Parallel Research Agent",
    description="Deep web research powered by Parallel Search, Claude, and Render Workflows.",
    version="0.1.0",
)

WORKFLOW_SLUG = os.environ.get("RENDER_WORKFLOW_SLUG", "")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
# Point at the local task server (default http://localhost:8120) to develop
# against `render workflows dev` instead of the hosted API.
RENDER_API_URL = os.environ.get("RENDER_API_URL", "https://api.render.com")


def _client() -> Render:
    return Render(token=RENDER_API_KEY or None, base_url=RENDER_API_URL)

TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ResearchRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=5,
        max_length=2000,
        description="The research question to investigate.",
        json_schema_extra={
            "examples": [
                "What are the leading open-source alternatives to Elasticsearch in 2026?"
            ]
        },
    )


class ResearchResponse(BaseModel):
    run_id: str
    status: str
    message: str


class RunStatus(BaseModel):
    run_id: str
    status: str
    result: dict | None = None


class BranchProgress(BaseModel):
    run_id: str
    status: str
    sub_question: str | None = None


class RunProgress(BaseModel):
    run_id: str
    status: str
    phase: str
    branches: list[BranchProgress] = []


_TERMINAL_OK = {"completed", "succeeded", "success"}
_TERMINAL_FAILED = {"failed", "errored", "error", "cancelled", "canceled"}


def _normalize_status(raw) -> str:
    """Collapse the SDK's run status onto the values the demo UI polls for.

    The status may arrive as a plain string or an enum depending on SDK
    version, so unwrap it before comparing.
    """
    value = getattr(raw, "value", raw)
    value = str(value).lower().rsplit(".", 1)[-1]

    if value in _TERMINAL_OK:
        return "completed"
    if value in _TERMINAL_FAILED:
        return "failed"
    return value


# A task run doesn't carry its task name, and the list endpoint can't filter by
# root, so we scan recent runs and identify children by their argument shape.
# That's fine for a demo; a production gateway would record child IDs itself.
RECENT_RUN_LIMIT = 100
_child_kinds: dict[str, tuple[str, str | None]] = {}


def _classify_child(client: Render, run_id: str) -> tuple[str, str | None]:
    """Identify a child run as planning, branch, or synthesis.

    The three subtasks are distinguishable by their arguments: plan_research
    takes (query), investigate takes (query, sub_question), and synthesize
    takes (query, branches). Results are cached since inputs never change.
    """
    cached = _child_kinds.get(run_id)
    if cached:
        return cached

    try:
        details = client.workflows.get_task_run(run_id)
    except Exception:
        return "unknown", None

    args = getattr(details, "input_", None) or []
    if isinstance(args, dict):
        args = list(args.values())

    kind, sub_question = "unknown", None
    if len(args) == 1:
        kind = "planning"
    elif len(args) >= 2 and isinstance(args[1], str):
        kind, sub_question = "branch", args[1]
    elif len(args) >= 2:
        kind = "synthesis"

    if kind != "unknown":
        _child_kinds[run_id] = (kind, sub_question)
    return kind, sub_question


def _phase(
    root_status: str,
    planning: str | None,
    synthesis: str | None,
    branches: list[BranchProgress],
) -> str:
    """Name the stage the pipeline is currently in, for the UI."""
    if root_status in ("completed", "failed"):
        return root_status
    if synthesis:
        return "synthesizing"
    if branches:
        return "investigating"
    if planning:
        return "planning"
    return "starting"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the demo form."""
    html_path = TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Parallel Research Agent</h1><p>POST to /research</p>")
    return HTMLResponse(html_path.read_text())


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/research", response_model=ResearchResponse)
async def start_research(req: ResearchRequest):
    """Dispatch a research_agent workflow run."""
    if not WORKFLOW_SLUG:
        raise HTTPException(
            status_code=503,
            detail=(
                "RENDER_WORKFLOW_SLUG is not set. Create the Workflow service "
                "in the Render Dashboard and set this env var to its slug."
            ),
        )

    client = _client()

    try:
        # Task identifier is "{workflow-slug}/{task-name}"; the input dict maps
        # to the task's named arguments (ctx is supplied by the runtime).
        run = client.workflows.start_task(
            f"{WORKFLOW_SLUG}/research_agent",
            {"query": req.query},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to dispatch run: {exc}")

    return ResearchResponse(
        run_id=run.id,
        status="dispatched",
        message=f"Research started. Poll GET /research/{run.id} for results.",
    )


@app.get("/research/{run_id}/progress", response_model=RunProgress)
async def get_progress(run_id: str):
    """Reconstruct live fan-out progress from the workflow run graph.

    Every subtask is its own run carrying `root_task_run_id`, so the whole
    pipeline is already observable without persisting anything ourselves.
    """
    client = _client()

    try:
        root = client.workflows.get_task_run(run_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {exc}")

    try:
        listed = client.workflows.list_task_runs(
            ListTaskRunsParams(limit=RECENT_RUN_LIMIT)
        )
    except Exception:
        listed = []

    branches: list[BranchProgress] = []
    planning: str | None = None
    synthesis: str | None = None

    for item in listed:
        child = getattr(item, "task_run", item)
        if child.id == run_id or getattr(child, "root_task_run_id", None) != run_id:
            continue

        kind, sub_question = _classify_child(client, child.id)
        status = _normalize_status(child.status)

        if kind == "branch":
            branches.append(
                BranchProgress(
                    run_id=child.id, status=status, sub_question=sub_question
                )
            )
        elif kind == "planning":
            planning = status
        elif kind == "synthesis":
            synthesis = status

    root_status = _normalize_status(root.status)
    branches.sort(key=lambda b: b.run_id)

    return RunProgress(
        run_id=run_id,
        status=root_status,
        phase=_phase(root_status, planning, synthesis, branches),
        branches=branches,
    )


@app.get("/research/{run_id}", response_model=RunStatus)
async def get_research(run_id: str):
    """Check the status of a research run."""
    client = _client()

    try:
        run = client.workflows.get_task_run(run_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {exc}")

    status = _normalize_status(run.status)

    result = None
    if status == "completed":
        results = getattr(run, "results", None)
        result = results if isinstance(results, dict) else None

    return RunStatus(
        run_id=run.id,
        status=status,
        result=result,
    )
