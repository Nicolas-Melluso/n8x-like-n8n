from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .actions import ACTION_REGISTRY
from .models import Workflow


class WorkflowEngine:
    def __init__(self, registry=None):
        self.registry = registry or ACTION_REGISTRY

    def run(
        self,
        workflow: Workflow,
        initial_context: Dict[str, Any],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        context: Dict[str, Any] = dict(initial_context)
        trace: List[Dict[str, Any]] = []
        run_started = time.perf_counter()
        total_steps = len(workflow.steps)

        for index, step in enumerate(workflow.steps, start=1):
            action = self.registry.get(step.action)
            if action is None:
                raise ValueError(f"Accion desconocida: {step.action}")

            if progress_callback:
                progress_callback(
                    {
                        "event": "step_started",
                        "step_id": step.id,
                        "action": step.action,
                        "index": index,
                        "total_steps": total_steps,
                    }
                )

            step_started = time.perf_counter()

            output = action(context, step.params)
            duration_ms = round((time.perf_counter() - step_started) * 1000, 2)

            if step.save_as:
                context[step.save_as] = output

            trace_item = {
                "step_id": step.id,
                "action": step.action,
                "save_as": step.save_as,
                "output_preview": str(output)[:300],
                "duration_ms": duration_ms,
            }
            trace.append(trace_item)

            if progress_callback:
                progress_callback(
                    {
                        "event": "step_completed",
                        "step_id": step.id,
                        "action": step.action,
                        "index": index,
                        "total_steps": total_steps,
                        "duration_ms": duration_ms,
                    }
                )

        return {
            "context": context,
            "trace": trace,
            "total_duration_ms": round((time.perf_counter() - run_started) * 1000, 2),
        }
