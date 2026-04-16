from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Step(BaseModel):
    id: str
    action: str
    params: Dict[str, Any] = Field(default_factory=dict)
    save_as: Optional[str] = None


class Workflow(BaseModel):
    id: str
    name: str
    description: str = ""
    steps: List[Step]


class RunInput(BaseModel):
    input: Dict[str, Any] = Field(default_factory=dict)
