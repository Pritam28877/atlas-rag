# Recovery tests

- `test_sqlite_dag_store.py`: restart-safe DAG leases and fenced checkpoints.
- `test_backup_restore.py`: content-addressed backup, SQLite snapshot, and restore verification.
- `test_upgrade.py`: schema writer gates and opaque rollback preservation.
- Recovery tests also cover descendant cancellation, stale workers, and paused ambiguous results.
