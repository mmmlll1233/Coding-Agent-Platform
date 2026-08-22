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

**Attempt Workspace**:
The disposable isolated filesystem owned by exactly one Attempt; every retry receives a new one.
_Avoid_: Job volume, shared workspace

**Repository Target**:
The GitHub repository and immutable base revision to which a Work Request applies.
_Avoid_: Repo info, source address

**Requester**:
An authenticated internal actor that submits a Work Request, supplies its Verification Contract, and answers requests for clarification.
_Avoid_: User, client, caller

**Verification Contract**:
The complete set of Requester-supplied checks that a proposed change must pass before it can become a Delivery.
_Avoid_: Test command, suggested tests

**Verification**:
The collected evidence that a proposed change satisfies every check in its Verification Contract.
_Avoid_: Testing, validation

**Delivery**:
The reviewable code change produced for a successful Job; for the MVP, a Delivery is a GitHub Draft Pull Request.
_Avoid_: Result, output, patch

**Needs Input**:
A non-terminal Job condition in which MewCode cannot continue safely without clarification from the Requester.
_Avoid_: Failed, blocked

**Failed**:
A stopped Job outcome in which MewCode could not produce a verified Delivery; automatic processing has ended, but the Requester may explicitly reopen the same Job with a new Attempt.
_Avoid_: Needs Input, error
