"""Optional native task owner shared by copied contexts and actual cleanup."""

from contextlib import contextmanager
from contextvars import ContextVar

_OWNER = ContextVar("native_agent_task_owner", default=None)


@contextmanager
def task_operation_owner(owner):
    token = _OWNER.set(owner)
    try:
        yield
    finally:
        _OWNER.reset(token)


def current_task_owner():
    return _OWNER.get()


def check_task_operation():
    owner = _OWNER.get()
    if owner is not None:
        owner.current()


def retain_task_operation(operation, *, cleanup=False):
    owner = _OWNER.get()
    if owner is not None:
        owner.retain(operation, cleanup=cleanup)
    return operation


def task_cleanup_unknown():
    owner = _OWNER.get()
    if owner is not None:
        owner.mark_unknown()
