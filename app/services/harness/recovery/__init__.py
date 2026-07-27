"""Fail-closed startup recovery composition."""

from app.services.harness.recovery.background_job_retention import (
    BackgroundJobCleanupReport,
    BackgroundJobRetentionCleaner,
)
from app.services.harness.recovery.background_job_startup import (
    BackgroundJobStartupRecovery,
    BackgroundJobStartupReport,
)
from app.services.harness.recovery.backup_restore import (
    BackupComponent,
    BackupComponentKind,
    BackupManifest,
    RestoreVerification,
    backup_manifest_sha256,
    build_backup_manifest,
    component_from_path,
    snapshot_sqlite_database,
    verify_restore,
)
from app.services.harness.recovery.coordinator import (
    RecoveryCoordinator,
    RecoveryCoordinatorError,
    RecoveryCoordinatorErrorCode,
    RecoveryEvidenceStore,
    RecoveryIntegrityVerifier,
    RecoveryProjectionRebuilder,
    StartupRecoveryReport,
    StartupRecoveryStatus,
)
from app.services.harness.recovery.sqlite_dag_store import SQLiteDurableDagStore
from app.services.harness.recovery.upgrade import (
    UpgradePlan,
    WriterGateDecision,
    plan_upgrade,
    plan_writer_gate,
    preserve_rollback_record,
)

__all__ = (
    "RecoveryCoordinator",
    "BackgroundJobCleanupReport",
    "BackgroundJobRetentionCleaner",
    "BackgroundJobStartupRecovery",
    "BackgroundJobStartupReport",
    "BackupComponent",
    "BackupComponentKind",
    "BackupManifest",
    "RestoreVerification",
    "backup_manifest_sha256",
    "build_backup_manifest",
    "component_from_path",
    "snapshot_sqlite_database",
    "verify_restore",
    "RecoveryCoordinatorError",
    "RecoveryCoordinatorErrorCode",
    "RecoveryEvidenceStore",
    "RecoveryIntegrityVerifier",
    "RecoveryProjectionRebuilder",
    "StartupRecoveryReport",
    "StartupRecoveryStatus",
    "SQLiteDurableDagStore",
    "UpgradePlan",
    "WriterGateDecision",
    "plan_upgrade",
    "plan_writer_gate",
    "preserve_rollback_record",
)
