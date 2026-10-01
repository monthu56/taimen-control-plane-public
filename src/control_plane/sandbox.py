"""What the core changes about itself while a package test runs its code (CP-ADR-0074 Z2).

The tests of a ``WorkRule`` and a ``TaskType`` (``POST /packages:test``) run
the application code of the core — the same commands the worker and the API
run — in a transaction that is always rolled back. Four things of the
process differ for that code while :func:`trial` is active, and only for the
task that entered it (context variables, never globals):

- **time** — :func:`control_plane.application.common.utcnow` is the virtual
  clock of the test: its ``given.clock``, one microsecond later at every
  read, so the order of what the code writes stays the order it wrote it;
- **task numbers** — a new task takes its public id from the test
  (``TEST-000001``, …), not from the tenant's counter row, which a test
  would otherwise hold locked until its rollback;
- **authorization** — the local authorizer decides, whatever ``CP_AUTHZ_MODE``
  says: the PDP knows neither the temporary principals of the test nor its
  transaction. One exception: in ``policy`` mode a *read* by the caller of
  the test (its IAM subject, :attr:`Trial.policy_subjects`) of a resource
  the test did not write (:attr:`Trial.written`) is still decided by the
  PDP, so a test never reads what its caller could not;
- **nothing leaves the process** — a call to memory, the content store or
  the PDP is refused with :class:`SandboxOutgoingCall` before it is made
  (the PDP question above is the one call let through, :func:`permit`).

Nothing here knows what a rule or a task type is; the runner of the tests
(:mod:`control_plane.application.commands.package_trials`) sets it up.
"""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SANDBOX_OUTGOING_CALL = "sandbox_outgoing_call"


class SandboxOutgoingCall(Exception):
    """A package test tried to reach outside its transaction.

    Not a domain error on purpose: no command turns it into a verdict of its
    own (a failed evaluation, a failed outcome); it ends the test.
    """

    def __init__(self, target: str) -> None:
        super().__init__(f"a package test makes no outgoing call ({target})")
        self.code = SANDBOX_OUTGOING_CALL
        self.target = target


@dataclass
class Trial:
    """The virtual clock and the task numbers of one test."""

    clock: datetime
    prefix: str = "TEST"
    tick: timedelta = timedelta(microseconds=1)
    numbers: int = 0
    # Every outgoing call the code attempted: ``noSideEffects`` reads it.
    outgoing: list[str] = field(default_factory=list)
    # The IAM subjects whose reads the PDP still decides: the caller's.
    policy_subjects: frozenset[str] = frozenset()
    # The ids of the rows the test inserted: the PDP has never heard of them.
    written: set[str] = field(default_factory=set)
    # The outgoing calls let through right now (:func:`permit`).
    permitted: set[str] = field(default_factory=set)

    def now(self) -> datetime:
        self.clock += self.tick
        return self.clock

    def next_public_id(self) -> str:
        self.numbers += 1
        return f"{self.prefix}-{self.numbers:06d}"

    def asks_policy(
        self, subject: str | None, actions: Iterable[str], resource: str | None
    ) -> bool:
        """Does the PDP decide this question, not the local check?

        Only a read (every action ``*.read``) by a subject of
        :attr:`policy_subjects` of a resource that is not a row of the test.
        """
        return (
            subject is not None
            and subject in self.policy_subjects
            and all(action.endswith(".read") for action in actions)
            and (resource is None or resource not in self.written)
        )


_trial: ContextVar[Trial | None] = ContextVar("package_trial", default=None)


def active() -> Trial | None:
    return _trial.get()


@contextmanager
def trial(state: Trial) -> Iterator[Trial]:
    """Run the enclosed code as the code of a package test."""
    token = _trial.set(state)
    try:
        yield state
    finally:
        _trial.reset(token)


def virtual_now() -> datetime | None:
    state = _trial.get()
    return state.now() if state is not None else None


def public_id_source() -> Callable[[], str] | None:
    state = _trial.get()
    return state.next_public_id if state is not None else None


def refuse_outgoing(target: str) -> None:
    """Called before any call that leaves the process: refused inside a test."""
    state = _trial.get()
    if state is not None and target not in state.permitted:
        state.outgoing.append(target)
        raise SandboxOutgoingCall(target)


@contextmanager
def permit(target: str) -> Iterator[None]:
    """Let the calls to ``target`` through for the enclosed code (a read the PDP decides)."""
    state = _trial.get()
    if state is None or target in state.permitted:
        yield
        return
    state.permitted.add(target)
    try:
        yield
    finally:
        state.permitted.discard(target)
