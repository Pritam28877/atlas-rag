"""Bounded checksummed security corpus tests."""

import hashlib
import json
from pathlib import Path

CORPUS_DIRECTORY = Path(__file__).parent / "corpus"
CORPUS_PATH = CORPUS_DIRECTORY / "escape_cases.json"
CHECKSUM_PATH = CORPUS_DIRECTORY / "escape_cases.sha256"
COVERAGE_PATH = CORPUS_DIRECTORY / "coverage.json"
MAXIMUM_CORPUS_BYTES = 32 * 1024
EXPECTED_CASE_IDS = {
    "environment.parent_secret",
    "extension.mcp_process",
    "extension.plugin_process",
    "filesystem.declared_write",
    "filesystem.host_path",
    "filesystem.path_traversal",
    "filesystem.symlink_escape",
    "filesystem.undeclared_write",
    "network.direct_egress",
    "network.redirect_without_proxy",
    "output.combined_limit",
    "output.secret_canary",
    "parser.document_process",
    "process.descendant_after_cancel",
    "system.host_block_device",
}


def test_security_corpus_is_bounded_complete_and_checksummed() -> None:
    encoded = CORPUS_PATH.read_bytes()
    assert len(encoded) <= MAXIMUM_CORPUS_BYTES
    payload = json.loads(encoded)
    assert payload["schema_version"] == 1
    cases = payload["cases"]
    case_ids = tuple(case["id"] for case in cases)
    assert tuple(sorted(set(case_ids))) == case_ids
    assert set(case_ids) == EXPECTED_CASE_IDS
    assert all(
        set(case) == {"id", "category", "expected"}
        for case in cases
    )

    expected_checksum = CHECKSUM_PATH.read_text(encoding="ascii").strip()
    assert expected_checksum == hashlib.sha256(encoded).hexdigest()

    coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    assert set(coverage) == EXPECTED_CASE_IDS
    repository_root = Path(__file__).resolve().parents[3]
    for node_id in coverage.values():
        test_path, separator, test_name = node_id.partition("::")
        assert separator and test_name.startswith("test_")
        source_path = repository_root / test_path
        assert source_path.is_file()
        source = source_path.read_text(encoding="utf-8")
        assert f"def {test_name}(" in source
