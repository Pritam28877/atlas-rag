# Atlas Harness workspace

This directory contains the Rust trusted-kernel workspace described in
[`docs/harness`](../docs/harness/). It is intentionally separate from the
existing Python RAG service. The RAG service may later be consumed through the
typed document adapter; it is not the harness executor.

The P3 workspace is a dependency-free boundary skeleton. It registers no RPC,
provider, tool, filesystem, process, MCP, plugin, remote-control, or telemetry
operation. Later PRs must preserve the dependency direction documented below.

## Toolchain

- Rust `1.95.0`
- Cargo `1.95.0`
- rustfmt, clippy, and rust-src from the pinned toolchain
- `cargo-deny 0.20.2` and `cargo-audit 0.22.2` in CI
- `cargo-cyclonedx 0.5.9` for release SBOM generation

## Dependency direction

```text
protocol
  ^
events <- core <- app-server <- cli
  ^        ^
provider  context
policy <- sandbox
tools <- extensions
scheduler <- memory
documents
observe <- eval
```

The diagram shows intended authority, not current Cargo edges. P3 crates are
dependency-free. A trusted-kernel crate must never depend on app-server, CLI,
SDK, IDE, or another client. Provider adapters cannot depend on the turn engine;
all outbound traffic later crosses the egress interface owned by core/policy.

## Local verification

```bash
cd atlas-harness
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --all-targets --locked
RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps --locked
python3 ../scripts/verify_harness_provenance.py
python3 ../scripts/verify_codex_rpc_inventory.py
```

Release/audit tooling:

```bash
cargo install cargo-deny --version 0.20.2 --locked
cargo install cargo-audit --version 0.22.2 --locked
cargo install cargo-cyclonedx --version 0.5.9 --locked
cargo deny check
cargo audit
cargo cyclonedx --all --format json
```

## Distribution status

Distribution is disabled. See [`LICENSE-PENDING.md`](LICENSE-PENDING.md) and
[`NOTICE.template`](NOTICE.template). Human approval of the outbound license
remains a release gate.
