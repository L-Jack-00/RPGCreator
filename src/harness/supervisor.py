# -*- coding: utf-8 -*-
"""
Harness Supervisor & Worker Rig (Pillar 2, 4, 6, 8)
Manages asynchronous execution, zero-load pause, emergency stops,
and physical input safety.
"""
import sys
import time
import threading
from typing import Optional, Callable
from ..core.models import HarnessState, JobContext
from ..core.engine import CoreEngine

# UTF-8 Console encoding safety guard (safe in headless / --noconsole frozen builds)
if sys.stdout is not None and getattr(sys.stdout, 'encoding', None) != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

class HarnessSupervisor:
    def __init__(self, engine: CoreEngine):
        self.engine = engine
        self._state = HarnessState.IDLE
        self._pause_event = threading.Event()
        self._pause_event.set()  # Not paused by default
        self._stop_requested = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    @property
    def state(self) -> HarnessState:
        return self._state

    def pause(self) -> None:
        """Pillar 4: Event-driven idle pause without busy-waiting."""
        self._pause_event.clear()
        self._state = HarnessState.PAUSED

    def resume(self) -> None:
        """Resumes execution from paused state."""
        self._pause_event.set()
        self._state = HarnessState.RUNNING

    def reset(self) -> None:
        """Resets supervisor interlocks for a new job execution."""
        self._stop_requested.clear()
        self._pause_event.set()
        self._state = HarnessState.IDLE

    def emergency_stop(self) -> None:
        """Pillar 4 & 6: Immediate emergency stop interlock."""
        self._stop_requested.set()
        self._pause_event.set()  # Unblock if paused to allow immediate exit
        self._state = HarnessState.STOPPED

    def run_job_sync(self, context: JobContext, total_steps: int = 10, step_delay_sec: float = 0.01) -> bool:
        """Synchronous run with emergency interlocks and zero-load pause guard."""
        if self._stop_requested.is_set():
            self._state = HarnessState.STOPPED
            return False

        self._state = HarnessState.RUNNING
        
        try:
            for step in range(total_steps):
                # 1. Emergency stop check
                if self._stop_requested.is_set():
                    self._state = HarnessState.STOPPED
                    return False
                
                # 2. Zero-load pause check (blocks cleanly without CPU spinning)
                self._pause_event.wait()
                
                # 3. Step execution
                self.engine.execute_step(context, step, total_steps)
                
                if step_delay_sec > 0:
                    time.sleep(step_delay_sec)
                    
            self._state = HarnessState.IDLE
            return True
        except Exception as e:
            self._state = HarnessState.ERROR
            context.status = HarnessState.ERROR
            context.error_message = str(e)
            return False
        finally:
            # Physical safety guard cleanup
            self._stop_requested.set()
