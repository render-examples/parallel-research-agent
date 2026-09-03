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
from render_sdk import Render

app = FastAPI(
    title="Parallel Research Agent",
    description="Deep web research powered by Parallel Search, Claude, and Render Workflows.",
    version="0.1.0",
)

WORKFLOW_SLUG = os.environ.get("RENDER_WORKFLOW_SLUG", "")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")

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

    client = Render(api_key=RENDER_API_KEY)

    try:
        run = client.workflows.start_task_run(
            workflow_slug=WORKFLOW_SLUG,
            task_name="research_agent",
            input={"query": req.query},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to dispatch run: {exc}")

    return ResearchResponse(
        run_id=run.id,
        status="dispatched",
        message=f"Research started. Poll GET /research/{run.id} for results.",
    )


@app.get("/research/{run_id}", response_model=RunStatus)
async def get_research(run_id: str):
    """Check the status of a research run."""
    client = Render(api_key=RENDER_API_KEY)

    try:
        run = client.workflows.get_task_run(run_id=run_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {exc}")

    result = None
    if run.status == "completed" and run.output:
        result = run.output

    return RunStatus(
        run_id=run.id,
        status=run.status,
        result=result,
    )
