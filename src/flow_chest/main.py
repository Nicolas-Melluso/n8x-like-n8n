from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from .engine import WorkflowEngine
from .models import RunInput, Workflow

app = FastAPI(title="Flow Chest", version="0.1.0")
engine = WorkflowEngine()
RUNS: Dict[str, Dict[str, Any]] = {}
RUNS_LOCK = threading.Lock()

# Load local environment variables from .env if present.
load_dotenv()


def _static_dir() -> Path:
    return Path(__file__).parent / "static"


app.mount("/static", StaticFiles(directory=_static_dir()), name="static")


def _workflows_dir() -> Path:
    folder = os.getenv("WORKFLOWS_DIR", "workflows")
    return Path(folder)


def load_workflows() -> Dict[str, Workflow]:
    folder = _workflows_dir()
    if not folder.exists():
        return {}

    found: Dict[str, Workflow] = {}
    for file in folder.glob("*.json"):
        with file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        wf = Workflow.model_validate(data)
        found[wf.id] = wf

    return found


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_run(run_id: str, updates: Dict[str, Any]) -> None:
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if run is None:
            return
        run.update(updates)


def _append_run_step(run_id: str, step_data: Dict[str, Any]) -> None:
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if run is None:
            return
        run.setdefault("completed_steps", []).append(step_data)


def _run_workflow_job(run_id: str, workflow: Workflow, initial_input: Dict[str, Any]) -> None:
    total_steps = len(workflow.steps)

    _update_run(
        run_id,
        {
            "status": "running",
            "started_at": _now_iso(),
            "total_steps": total_steps,
            "progress_pct": 0,
        },
    )

    def on_progress(event: Dict[str, Any]) -> None:
        event_name = event.get("event")
        if event_name == "step_started":
            index = int(event.get("index", 1))
            pct = int(((index - 1) / max(total_steps, 1)) * 100)
            _update_run(
                run_id,
                {
                    "current_step": event.get("step_id"),
                    "current_step_action": event.get("action"),
                    "progress_pct": pct,
                },
            )
        if event_name == "step_completed":
            index = int(event.get("index", 1))
            pct = int((index / max(total_steps, 1)) * 100)
            _append_run_step(
                run_id,
                {
                    "step_id": event.get("step_id"),
                    "action": event.get("action"),
                    "duration_ms": event.get("duration_ms", 0),
                },
            )
            _update_run(run_id, {"progress_pct": pct})

    try:
        result = engine.run(workflow, initial_input, progress_callback=on_progress)
        _update_run(
            run_id,
            {
                "status": "completed",
                "finished_at": _now_iso(),
                "progress_pct": 100,
                "result": result,
                "error": None,
                "current_step": None,
                "current_step_action": None,
            },
        )
    except Exception as exc:
        _update_run(
            run_id,
            {
                "status": "failed",
                "finished_at": _now_iso(),
                "error": str(exc),
                "current_step": None,
                "current_step_action": None,
            },
        )


@app.get("/")
def root() -> dict:
    return {
        "name": "Flow Chest",
        "status": "running",
        "docs": "/docs",
        "health": "/health",
        "workflows": "/workflows",
        "ui": "/ui",
    }


@app.get("/ui")
def ui() -> FileResponse:
    return FileResponse(_static_dir() / "index.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/workflows")
def list_workflows() -> dict:
    workflows = load_workflows()
    return {
        "count": len(workflows),
        "items": [
            {
                "id": wf.id,
                "name": wf.name,
                "description": wf.description,
                "steps": len(wf.steps),
            }
            for wf in workflows.values()
        ],
    }


@app.post("/run/{workflow_id}")
def run_workflow(workflow_id: str, payload: RunInput) -> dict:
    workflows = load_workflows()
    workflow = workflows.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow no encontrado")

    try:
        result = engine.run(workflow, payload.input)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "workflow_id": workflow_id,
        "result": result,
    }


@app.post("/run-async/{workflow_id}")
def run_workflow_async(workflow_id: str, payload: RunInput) -> dict:
    workflows = load_workflows()
    workflow = workflows.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow no encontrado")

    run_id = str(uuid4())
    with RUNS_LOCK:
        RUNS[run_id] = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "status": "queued",
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "progress_pct": 0,
            "total_steps": len(workflow.steps),
            "current_step": None,
            "current_step_action": None,
            "completed_steps": [],
            "result": None,
            "error": None,
        }

    thread = threading.Thread(
        target=_run_workflow_job,
        args=(run_id, workflow, payload.input),
        daemon=True,
    )
    thread.start()

    return {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "status": "queued",
        "status_url": f"/runs/{run_id}",
    }


@app.get("/runs/{run_id}")
def get_run_status(run_id: str) -> dict:
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run no encontrado")
        return run
