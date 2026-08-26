# Coding Agent Platform

This context describes the language used to accept, execute, and deliver autonomous coding work through MewCode.

## Language

**Work Request**:
An accepted unit of intent describing one bug fix or small code change against one repository.
_Avoid_: Bug, ticket, prompt, requirement

**Job**:
The durable lifecycle through which MewCode processes one Work Request to a terminal outcome.
_Avoid_: Task, session, conversation

**Attempt**:
One execution of a Job; a retried Job receives a new Attempt while retaining the same identity.
_Avoid_: Run, retry task

**Worker Lease**:
The time-bounded right for one Worker to advance an Attempt; an expired lease cannot authorize later state changes.
_Avoid_: Lock, ownership flag

**Platform Capacity**:
The maximum number of leased Attempts that may be active across all Workers at one time.
_Avoid_: Worker concurrency, thread count

**Worker Slot**:
One unit of a Worker's local ability to execute an Attempt, bounded independently from Platform Capacity.
_Avoid_: Platform Capacity, worker thread

**Worker Drain**:
A Worker condition that refuses new Attempts while preserving active Worker Leases until completion or a bounded shutdown grace expires.
_Avoid_: Immediate shutdown, cancellation

**Attempt Workspace**:
The disposable isolated filesystem owned by exactly one Attempt; every retry receives a new one.
_Avoid_: Job volume, shared workspace

**Repository Target**:
The GitHub repository and immutable base revision to which a Work Request applies.
_Avoid_: Repo info, source address

**Prepared Repository**:
A trusted, credential-free snapshot of a Repository Target together with the manifest needed to prove later Workspace changes.
_Avoid_: Checkout, clone, working copy

**Repository Size**:
The total payload bytes of regular files and symbolic-link targets in a normalized Prepared Repository at its immutable base revision.
_Avoid_: Git history size, compressed archive size, Workspace size

**Attempt Deadline**:
The maximum active processing time granted independently to one Attempt; queued time and time awaiting Requester input are outside it.
_Avoid_: Job lifetime, command timeout

**Requester**:
An authenticated internal actor that submits a Work Request, supplies its Verification Contract, and answers requests for clarification.
_Avoid_: User, client, caller

**Verification Contract**:
The complete set of Requester-supplied checks that a proposed change must pass before it can become a Delivery.
_Avoid_: Test command, suggested tests

**Verification**:
The collected evidence that a proposed change satisfies every check in its Verification Contract.
_Avoid_: Testing, validation

**Repair Round**:
A bounded Agent modification cycle triggered by failed Verification before the complete Verification Contract is run again.
_Avoid_: Retry, test rerun

**Artifact**:
Redacted execution evidence retained for an Attempt outside its disposable Attempt Workspace.
_Avoid_: Attachment, workspace file, result

**Delivery**:
The reviewable code change produced for a successful Job; for the MVP, a Delivery is a GitHub Draft Pull Request.
_Avoid_: Result, output, patch

**Notification**:
An externally visible status card derived from exactly one persisted Job Event and identified by a stable Notification ID.
_Avoid_: Delivery, Job Event, message

**Notification Delivery Attempt**:
One Notifier call to the external notification platform for a Notification; it is independent of a Job Attempt.
_Avoid_: Attempt, retry task

**Needs Input**:
A non-terminal Job condition in which MewCode cannot continue safely without clarification from the Requester.
_Avoid_: Failed, blocked

**Failed**:
A stopped Job outcome in which MewCode could not produce a verified Delivery; automatic processing has ended, but the Requester may explicitly reopen the same Job with a new Attempt.
_Avoid_: Needs Input, error
