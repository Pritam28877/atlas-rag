# Scheduler tests

- `test_background_job_contracts.py`: job bounds and lifecycle invariants.
- `test_sqlite_background_job_store.py`: durable ownership and cleanup.
- `test_background_job_coordinator.py`: execution, artifacts, and cancellation.
- `test_background_job_output_bounds.py`: fail-closed artifact output limits.
- `test_background_job_retention.py`: bounded TTL job cleanup.
- `test_background_job_startup.py`: restart resume and reconciliation.
- `test_dag_compiler.py`: bounded graph validation and ready waves.
- `test_durable_dag.py`: lease fencing and dependency checkpoints.
- `fixtures.py`: shared durable job records and operation setup.
