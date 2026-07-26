"""Canonical event names emitted by Atlas-owned harness components."""

from enum import StrEnum


class CanonicalEventType(StrEnum):
    GRANT_ISSUED = "Grant.Issued"
    GRANT_REVOKED = "Grant.Revoked"
    GRANT_EXPIRED = "Grant.Expired"
    POLICY_DECISION_RECORDED = "Policy.DecisionRecorded"

    WORKSPACE_OPENED = "Workspace.Opened"
    WORKSPACE_CLOSED = "Workspace.Closed"

    THREAD_CREATED = "Thread.Created"
    THREAD_RESUMED = "Thread.Resumed"
    THREAD_FORKED = "Thread.Forked"
    THREAD_ARCHIVED = "Thread.Archived"

    ITEM_CREATED = "Item.Created"
    ITEM_REDACTED = "Item.Redacted"

    TURN_ACCEPTED = "Turn.Accepted"
    TURN_STARTED = "Turn.Started"
    TURN_STEERED = "Turn.Steered"
    TURN_CANCELLATION_REQUESTED = "Turn.CancellationRequested"
    TURN_COMPACTION_REQUESTED = "Turn.CompactionRequested"
    TURN_COMPACTED = "Turn.Compacted"
    TURN_COMPLETED = "Turn.Completed"
    TURN_FAILED = "Turn.Failed"
    TURN_CANCELLED = "Turn.Cancelled"
    CONTEXT_COMPILED = "Context.Compiled"

    APPROVAL_REQUESTED = "Approval.Requested"
    APPROVAL_APPROVED = "Approval.Approved"
    APPROVAL_DENIED = "Approval.Denied"
    APPROVAL_EXPIRED = "Approval.Expired"
    APPROVAL_CANCELLED = "Approval.Cancelled"

    TASK_READY = "Task.Ready"
    TASK_STARTED = "Task.Started"
    TASK_BLOCKED = "Task.Blocked"
    TASK_RETRIED = "Task.Retried"
    TASK_CANCELLATION_REQUESTED = "Task.CancellationRequested"
    TASK_COMPLETED = "Task.Completed"
    TASK_FAILED = "Task.Failed"
    TASK_CANCELLED = "Task.Cancelled"

    OPERATION_PREPARED = "Operation.Prepared"
    OPERATION_DISPATCHED = "Operation.Dispatched"
    OPERATION_COMPLETED = "Operation.Completed"
    OPERATION_FAILED = "Operation.Failed"
    OPERATION_AMBIGUOUS = "Operation.Ambiguous"
    OPERATION_CANCELLED = "Operation.Cancelled"

    ARTIFACT_STAGED = "Artifact.Staged"
    ARTIFACT_DURABLE = "Artifact.Durable"
    ARTIFACT_QUARANTINED = "Artifact.Quarantined"
    ARTIFACT_DELETED = "Artifact.Deleted"

    PROVIDER_DECISION_RECORDED = "Provider.DecisionRecorded"
    PROVIDER_ATTEMPT_STARTED = "Provider.AttemptStarted"
    PROVIDER_DELTA_BATCHED = "Provider.DeltaBatched"
    PROVIDER_USAGE_RECORDED = "Provider.UsageRecorded"
    PROVIDER_ATTEMPT_COMPLETED = "Provider.AttemptCompleted"
    PROVIDER_ATTEMPT_FAILED = "Provider.AttemptFailed"
    PROVIDER_ATTEMPT_CANCELLED = "Provider.AttemptCancelled"

    EVALUATION_ACCEPTED = "Evaluation.Accepted"
    EVALUATION_STARTED = "Evaluation.Started"
    EVALUATION_COMPLETED = "Evaluation.Completed"
    EVALUATION_FAILED = "Evaluation.Failed"
    EVALUATION_CANCELLED = "Evaluation.Cancelled"

    EVENT_SUBSCRIPTION_STARTED = "Event.SubscriptionStarted"
    EVENT_ACKNOWLEDGED = "Event.Acknowledged"
    EVENT_RESYNC_REQUIRED = "Event.ResyncRequired"

    RECOVERY_STARTED = "Recovery.Started"
    RECOVERY_REPLAYED = "Recovery.Replayed"
    RECOVERY_RECONCILED = "Recovery.Reconciled"
    RECOVERY_FAILED = "Recovery.Failed"

    USAGE_UPDATED = "Usage.Updated"
