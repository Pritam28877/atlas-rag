# Scheduler tests

- `test_background_job_contracts.py`: job bounds and lifecycle invariants.
- `test_sqlite_background_job_store.py`: durable ownership and cleanup.
- `test_background_job_coordinator.py`: execution, artifacts, and cancellation.
- `test_background_job_output_bounds.py`: fail-closed artifact output limits.
- `fixtures.py`: shared durable job records and operation setup.
