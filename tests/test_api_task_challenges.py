from __future__ import annotations

import threading
import time
import uuid

from application.tasks import TASK_STATUS_RUNNING, get_task
from core.db import TaskModel, engine
from core.registration import ChallengeRequest, ChallengeResponse
from core.task_challenges import request_human_challenge
from sqlmodel import Session


def _create_running_task(platform: str) -> str:
    task_id = uuid.uuid4().hex
    with Session(engine) as session:
        model = TaskModel(
            id=task_id,
            type="register",
            platform=platform,
            status=TASK_STATUS_RUNNING,
            payload_json="{}",
            progress_total=1,
        )
        session.add(model)
        session.commit()
    return task_id


def _wait_for_challenge(task_id: str, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = get_task(task_id) or {}
        challenge = (task.get("result") or {}).get("challenge")
        if challenge:
            return challenge
        time.sleep(0.01)
    raise AssertionError("challenge was not published")


def test_api_can_resolve_active_human_challenge(client):
    task_id = _create_running_task("kimi")
    holder: dict[str, ChallengeResponse] = {}

    def wait_for_user() -> None:
        holder["response"] = request_human_challenge(
            task_id,
            ChallengeRequest(kind="oauth_confirmation", message="Complete Google login"),
            timeout=2,
        )

    thread = threading.Thread(target=wait_for_user)
    thread.start()
    challenge = _wait_for_challenge(task_id)

    response = client.post(
        f"/api/tasks/{task_id}/challenge",
        json={
            "completed": True,
            "challenge_id": challenge["id"],
        },
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert holder["response"].completed is True


def test_api_rejects_missing_or_stale_human_challenge(client):
    missing = client.post(
        "/api/tasks/does-not-exist/challenge",
        json={"completed": True, "challenge_id": "missing"},
    )
    assert missing.status_code == 404

    task_id = _create_running_task("zai")
    stale = client.post(
        f"/api/tasks/{task_id}/challenge",
        json={"completed": True, "challenge_id": "stale"},
    )
    assert stale.status_code == 409
