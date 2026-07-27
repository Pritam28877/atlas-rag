# Recovery tests

- `test_sqlite_dag_store.py`: restart-safe DAG leases and fenced checkpoints.
- Recovery tests also cover descendant cancellation, stale workers, and paused ambiguous results.
