# Conformance tests

- `fixtures.py`: bounded synthetic adapters and events.
- `mock_provider.py`: canonical mock fixtures.
- `native_cloud.py`: Bedrock and Vertex fixtures.
- `openai_family.py`: OpenAI, OpenRouter, and local fixtures.
- `test_native_cloud.py`: native/cloud equivalence.
- `test_full_matrix.py`: six-provider equivalence.
- `test_openai_family.py`: OpenAI-family equivalence.
- `test_outcome.py`: semantic event normalization.
- `test_recorded.py`: checksum-pinned decoder replay.
- `test_runner.py`: execution, equivalence, and failure checks.
