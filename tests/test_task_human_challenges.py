from __future__ import annotations

import threading
import time

from core.registration import ChallengeRequest, ChallengeResponse


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_task_human_challenge_waits_for_user_and_resumes():
    from application.tasks import (
        TASK_STATUS_RUNNING,
        TASK_STATUS_WAITING_USER,
        create_task,
        get_task,
        request_human_challenge,
        resolve_task_challenge,
    )

    task = create_task(
        task_type="register",
        platform="zai",
        payload={"platform": "zai"},
        progress_total=1,
    )
    task_id = task["task_id"]

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

    assert _wait_until(lambda: (get_task(task_id) or {}).get("status") == TASK_STATUS_WAITING_USER)
    waiting = get_task(task_id)
    assert waiting is not None
    assert waiting["terminal"] is False
    assert waiting["challenge"]["kind"] == "oauth_confirmation"
    assert waiting["challenge"]["url"] == "https://chat.z.ai/auth"
    challenge_id = waiting["challenge"]["id"]

    resolved = resolve_task_challenge(
        task_id,
        ChallengeResponse(completed=True),
        challenge_id=challenge_id,
    )
    assert resolved is not None

    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert holder["response"].completed is True

    resumed = get_task(task_id)
    assert resumed is not None
    assert resumed["status"] == TASK_STATUS_RUNNING
    assert resumed["challenge"] is None


def test_task_human_challenge_rejects_stale_challenge_id():
    from application.tasks import (
        TASK_STATUS_WAITING_USER,
        create_task,
        get_task,
        request_human_challenge,
        resolve_task_challenge,
    )

    task = create_task(
        task_type="register",
        platform="kimi",
        payload={"platform": "kimi"},
        progress_total=1,
    )
    task_id = task["task_id"]

    holder: dict[str, ChallengeResponse] = {}

    def run_challenge() -> None:
        holder["response"] = request_human_challenge(
            task_id,
            ChallengeRequest(kind="security_check", message="Complete Kimi security check"),
            timeout=2,
        )

    thread = threading.Thread(target=run_challenge)
    thread.start()
    assert _wait_until(lambda: (get_task(task_id) or {}).get("status") == TASK_STATUS_WAITING_USER)

    assert resolve_task_challenge(
        task_id,
        ChallengeResponse(completed=True),
        challenge_id="stale-id",
    ) is None
    assert (get_task(task_id) or {})["status"] == TASK_STATUS_WAITING_USER

    current_id = (get_task(task_id) or {})["challenge"]["id"]
    assert resolve_task_challenge(
        task_id,
        ChallengeResponse(completed=True),
        challenge_id=current_id,
    ) is not None
    thread.join(timeout=2)
    assert holder["response"].completed is True


def test_waiting_user_is_recovered_as_interrupted_after_restart():
    from application.tasks import (
        TASK_STATUS_INTERRUPTED,
        TASK_STATUS_WAITING_USER,
        create_task,
        get_task,
        mark_incomplete_tasks_interrupted,
    )
    from core.db import TaskModel, engine
    from sqlmodel import Session

    task = create_task(
        task_type="register",
        platform="zai",
        payload={"platform": "zai"},
        progress_total=1,
    )
    task_id = task["task_id"]
    with Session(engine) as session:
        model = session.get(TaskModel, task_id)
        assert model is not None
        model.status = TASK_STATUS_WAITING_USER
        session.add(model)
        session.commit()

    mark_incomplete_tasks_interrupted()
    assert (get_task(task_id) or {})["status"] == TASK_STATUS_INTERRUPTED
