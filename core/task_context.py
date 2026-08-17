from __future__ import annotations

import threading


_local = threading.local()


def set_current_task_id(task_id: str) -> None:
    _local.task_id = str(task_id or "").strip()


def get_current_task_id() -> str:
    return str(getattr(_local, "task_id", "") or "")


def clear_current_task_id() -> None:
    if hasattr(_local, "task_id"):
        delattr(_local, "task_id")
