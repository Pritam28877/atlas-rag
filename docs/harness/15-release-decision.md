# Atlas Harness release decision

Current decision: **BLOCKED**.

The decision is represented by `ReleaseDecision` in
`app/services/harness/evaluation/release.py`. It cannot become approved unless
there are no blockers, a human approver is recorded, and the release artifact
digest is present.

## Open blockers

| Code | Owner | Evidence required |
| --- | --- | --- |
| `live_provider_matrix` | Provider on-call | Two cloud providers and one local endpoint with capped redacted smokes (`P14.3`, `P15.3`) |
| `sandbox_independent_review` | Security on-call | Independent Linux/macOS/Windows review and any required native runner evidence |
| `supply_chain_release_blockers` | Platform lead | Resolve or formally approve the existing license/repository exceptions |
| `bounded_24h_soak` | Platform operator | Signed 24-hour resource/cost packet from `14-independent-review-and-soak.md` |
| `human_release_approval` | Release approver | Explicit approval tied to the candidate digest and rollback evidence |

## Decision procedure

1. Attach each blocker’s authoritative evidence and reviewer signature to the
   candidate revision.
2. Re-run the non-live and package gates, then execute the live, security, and
   soak procedures without changing their bounds.
3. Build a reproducible artifact and SBOM, record the SHA-256 digest, and run
   the rollback reader against the same journal without rewriting it.
4. Construct an approved `ReleaseDecision` only after a human signs the packet.
   Any missing, weak, or indirect evidence keeps the status blocked.
