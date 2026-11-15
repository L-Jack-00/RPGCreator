# -*- coding: utf-8 -*-
"""
Domain Models & Blackboard State (Pillar 1 & 3)
Pure domain types decoupled from I/O and external drivers.
"""
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Any, Optional

class HarnessState(Enum):
    IDLE = auto()
    INITIALIZING = auto()
    RUNNING = auto()
    PAUSED = auto()
    STOPPED = auto()
    ERROR = auto()

@dataclass
class JobContext:
    job_id: str
    status: HarnessState = HarnessState.IDLE
    payload: Dict[str, Any] = field(default_factory=dict)
    progress_pct: float = 0.0
    error_message: Optional[str] = None
