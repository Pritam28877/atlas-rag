# Harness security tests

- `corpus/`: checksummed cases and exact test coverage.
- `test_corpus_integrity.py`: corpus bounds and checksum.
- `test_disabled_hosts.py`: parser/MCP/plugin fail-closed state.
- `test_linux_escape_corpus.py`: disposable containment probes.
- `test_privileged_operations.py`: default-deny registry checks.
- `test_privileged_reachability.py`: callsite and bypass checks.
