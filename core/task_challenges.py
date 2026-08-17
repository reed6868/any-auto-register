from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
import uuid
from typing import Callable

from sqlmodel import Session

from core.db import TaskModel, engine
from core.registration.models import ChallengeRequest, ChallengeResponse


_STOPPED_TASK_STATUSES = {
    "cancel_requested",
    "cancelled",
    "failed",
    "interrupted",
    "succeeded",
}


@dataclass(slots=True)
class _ChallengeState:
    challenge_id: str
    event: threading.Event = field(default_factory=threading.Event)
    response: ChallengeResponse | None = None


_states: dict[str, _ChallengeState] = {}
_states_lock = threading.Lock()
_serial_locks: dict[str, threading.Lock] = {}
_serial_locks_guard = threading.Lock()


def _serial_lock(task_id: str) -> threading.Lock:
    with _serial_locks_guard:
        lock = _serial_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _serial_locks[task_id] = lock
        return lock


def _challenge_payload(challenge_id: str, request: ChallengeRequest) -> dict:
    return {
        "id": challenge_id,
        "kind": str(request.kind or "security_check"),
        "message": str(request.message or ""),
        "url": str(request.url or ""),
        "metadata": dict(request.metadata or {}),
    }


def _publish(task_id: str, challenge_id: str, request: ChallengeRequest) -> bool:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task or task.status in _STOPPED_TASK_STATUSES:
            return False
        result = task.get_result()
        result["challenge"] = _challenge_payload(challenge_id, request)
        task.set_result(result)
        session.add(task)
        session.commit()
        return True


def _clear(task_id: str, challenge_id: str) -> None:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task:
            return
        result = task.get_result()
        current = result.get("challenge")
        if isinstance(current, dict) and current.get("id") == challenge_id:
            result["challenge"] = None
            task.set_result(result)
            session.add(task)
            session.commit()


def _task_stopped(task_id: str) -> bool:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return bool(not task or task.status in _STOPPED_TASK_STATUSES)


def request_human_challenge(
    task_id: str,
    request: ChallengeRequest,
    *,
    timeout: float = 600,
) -> ChallengeResponse:
    """Publish a user-action challenge and block until it is resolved/cancelled.

    The challenge description is persisted in the existing task result JSON so
    web clients can display it. The user's response value stays in memory and
    is never persisted, which avoids storing transient 2FA/security material.
    """
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return ChallengeResponse(completed=False)

    with _serial_lock(normalized_task_id):
        challenge_id = uuid.uuid4().hex
        state = _ChallengeState(challenge_id=challenge_id)
        with _states_lock:
            _states[normalized_task_id] = state

        if not _publish(normalized_task_id, challenge_id, request):
            with _states_lock:
                _states.pop(normalized_task_id, None)
            return ChallengeResponse(completed=False)

        deadline = time.monotonic() + max(float(timeout or 0), 0.1)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ChallengeResponse(completed=False)
                if state.event.wait(timeout=min(0.25, remaining)):
                    return state.response or ChallengeResponse(completed=False)
                if _task_stopped(normalized_task_id):
                    return ChallengeResponse(completed=False)
        finally:
            _clear(normalized_task_id, challenge_id)
            with _states_lock:
                current = _states.get(normalized_task_id)
                if current is state:
                    _states.pop(normalized_task_id, None)


def resolve_task_challenge(
    task_id: str,
    response: ChallengeResponse,
    *,
    challenge_id: str = "",
) -> bool:
    """Resolve only the currently active challenge for a task."""
    normalized_task_id = str(task_id or "").strip()
    normalized_challenge_id = str(challenge_id or "").strip()
    with _states_lock:
        state = _states.get(normalized_task_id)
        if not state:
            return False
        if normalized_challenge_id and state.challenge_id != normalized_challenge_id:
            return False
        state.response = ChallengeResponse(
            completed=bool(response.completed),
            value=str(response.value or ""),
        )
        state.event.set()
        return True


def build_task_challenge_callback(
    task_id: str,
    *,
    timeout: float = 600,
) -> Callable[[ChallengeRequest], ChallengeResponse] | None:
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return None

    def _callback(request: ChallengeRequest) -> ChallengeResponse:
        return request_human_challenge(normalized_task_id, request, timeout=timeout)

    return _callback
