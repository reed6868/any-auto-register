from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from core.registration import ChallengeRequest, ChallengeResponse, RegistrationContext


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _mark_running(task_id: str) -> None:
    from application.tasks import TASK_STATUS_RUNNING
    from core.db import TaskModel, engine
    from sqlmodel import Session

    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        assert task is not None
        task.status = TASK_STATUS_RUNNING
        session.add(task)
        session.commit()


def _context(extra: dict | None = None, platform=None) -> RegistrationContext:
    return RegistrationContext(
        platform_name="zai",
        platform_display_name="Z.AI",
        platform=platform or SimpleNamespace(),
        identity=SimpleNamespace(),
        config=SimpleNamespace(executor_type="headed", proxy=None, extra=extra or {}),
        email=None,
        password=None,
        log_fn=lambda _message: None,
    )


def test_registration_context_only_builds_challenge_callback_from_bound_task_logger():
    from application.tasks import TaskLogger

    assert _context({"_task_id": "attacker-controlled"}).challenge_callback is None

    task_logger = TaskLogger("trusted-task")
    platform = SimpleNamespace(_log_fn=task_logger.log)
    assert callable(_context(platform=platform).challenge_callback)


def test_task_human_challenge_waits_for_user_and_clears_result():
    from application.tasks import TASK_STATUS_RUNNING, create_task, get_task
    from core.task_challenges import request_human_challenge, resolve_task_challenge

    task = create_task(
        task_type="register",
        platform="zai",
        payload={"platform": "zai"},
        progress_total=1,
    )
    task_id = task["task_id"]
    _mark_running(task_id)

    holder: dict[str, ChallengeResponse] = {}

    def run_challenge() -> None:
        holder["response"] = request_human_challenge(
            task_id,
            ChallengeRequest(
                kind="oauth_confirmation",
                message="Complete login in the visible browser",
                url="https://chat.z.ai/auth",
                metadata={"platform": "zai"},
            ),
            timeout=2,
        )

    thread = threading.Thread(target=run_challenge)
    thread.start()

    assert _wait_until(
        lambda: bool(((get_task(task_id) or {}).get("result") or {}).get("challenge"))
    )
    waiting = get_task(task_id)
    assert waiting is not None
    assert waiting["status"] == TASK_STATUS_RUNNING
    assert waiting["terminal"] is False
    challenge = waiting["result"]["challenge"]
    assert challenge["kind"] == "oauth_confirmation"
    assert challenge["url"] == "https://chat.z.ai/auth"

    resolved = resolve_task_challenge(
        task_id,
        ChallengeResponse(completed=True),
        challenge_id=challenge["id"],
    )
    assert resolved is True

    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert holder["response"].completed is True

    resumed = get_task(task_id)
    assert resumed is not None
    assert resumed["status"] == TASK_STATUS_RUNNING
    assert resumed["result"].get("challenge") is None


def test_task_human_challenge_rejects_stale_challenge_id():
    from application.tasks import create_task, get_task
    from core.task_challenges import request_human_challenge, resolve_task_challenge

    task = create_task(
        task_type="register",
        platform="kimi",
        payload={"platform": "kimi"},
        progress_total=1,
    )
    task_id = task["task_id"]
    _mark_running(task_id)

    holder: dict[str, ChallengeResponse] = {}

    def run_challenge() -> None:
        holder["response"] = request_human_challenge(
            task_id,
            ChallengeRequest(kind="security_check", message="Complete Kimi security check"),
            timeout=2,
        )

    thread = threading.Thread(target=run_challenge)
    thread.start()
    assert _wait_until(
        lambda: bool(((get_task(task_id) or {}).get("result") or {}).get("challenge"))
    )

    challenge = (get_task(task_id) or {})["result"]["challenge"]
    assert resolve_task_challenge(
        task_id,
        ChallengeResponse(completed=True),
        challenge_id="stale-id",
    ) is False

    assert resolve_task_challenge(
        task_id,
        ChallengeResponse(completed=True),
        challenge_id=challenge["id"],
    ) is True
    thread.join(timeout=2)
    assert holder["response"].completed is True


def test_task_human_challenge_stops_when_task_is_cancelled():
    from application.tasks import create_task, request_cancel
    from core.task_challenges import request_human_challenge

    task = create_task(
        task_type="register",
        platform="zai",
        payload={"platform": "zai"},
        progress_total=1,
    )
    task_id = task["task_id"]
    _mark_running(task_id)

    holder: dict[str, ChallengeResponse] = {}

    def run_challenge() -> None:
        holder["response"] = request_human_challenge(
            task_id,
            ChallengeRequest(kind="security_check"),
            timeout=2,
        )

    thread = threading.Thread(target=run_challenge)
    thread.start()
    time.sleep(0.05)
    request_cancel(task_id)
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert holder["response"].completed is False
