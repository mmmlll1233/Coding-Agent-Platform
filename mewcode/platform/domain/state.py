from __future__ import annotations

import re

from .models import AttemptStatus, JobStatus


class InvalidTransition(ValueError):
    pass


_JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.RECEIVED: frozenset({JobStatus.QUEUED, JobStatus.FAILED}),
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.NEEDS_INPUT,
            JobStatus.CANCEL_REQUESTED,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.QUEUED,
        }
    ),
    JobStatus.NEEDS_INPUT: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.CANCEL_REQUESTED: frozenset({JobStatus.CANCELLED}),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

_ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.QUEUED: frozenset({AttemptStatus.RUNNING, AttemptStatus.CANCELLED}),
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.COMPLETED,
            AttemptStatus.NEEDS_INPUT,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
        }
    ),
    AttemptStatus.COMPLETED: frozenset(),
    AttemptStatus.NEEDS_INPUT: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
}


def ensure_job_transition(
    current: JobStatus,
    target: JobStatus,
    *,
    pr_number: int | None = None,
    pr_url: str | None = None,
    head_branch: str | None = None,
    head_sha: str | None = None,
    verification_succeeded: bool = False,
) -> None:
    if target not in _JOB_TRANSITIONS[current]:
        raise InvalidTransition(f"Job cannot transition from {current} to {target}")
    pr_match = re.fullmatch(
        r"https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/([1-9][0-9]*)",
        pr_url.strip() if pr_url else "",
    )
    has_delivery_evidence = bool(
        pr_number is not None
        and pr_number > 0
        and pr_match is not None
        and int(pr_match.group(1)) == pr_number
        and head_branch
        and re.fullmatch(
            r"mewcode/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            head_branch,
        )
        and head_sha
        and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head_sha)
        and verification_succeeded
    )
    if target == JobStatus.SUCCEEDED and not has_delivery_evidence:
        raise InvalidTransition(
            "SUCCEEDED requires PR URL, PR number, owned branch, head SHA, and successful Verification"
        )


def ensure_attempt_transition(current: AttemptStatus, target: AttemptStatus) -> None:
    if target not in _ATTEMPT_TRANSITIONS[current]:
        raise InvalidTransition(f"Attempt cannot transition from {current} to {target}")
