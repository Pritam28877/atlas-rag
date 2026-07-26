# Atlas Harness provenance and license plan

Status: P0 proposal pending human legal approval. Review date: 2026-07-26.

This document defines which reviewed sources may influence Atlas and what must
be recorded before source is copied or substantially adapted. It is an
engineering control, not legal advice or a substitute for owner approval.

The authoritative machine-readable inventory is
[`provenance-manifest.json`](provenance-manifest.json). Validate it with:

```bash
uv run python scripts/verify_harness_provenance.py
```

## Outbound license proposal

Apache-2.0 remains the proposed Atlas Harness outbound license for the new
Python feature package, subject to compatibility review with this repository
and any future approved Apache-2.0 or MIT inputs. Codex is design evidence, not
the current code base, and no Codex Rust source is included. The proposal
remains `pending_human_approval`; this PR does not add a root `LICENSE`,
represent that legal review occurred, or relicense the existing RAG application.

Approval must record:

1. the repository/package boundary covered by the outbound license;
2. compatibility with all copied Apache-2.0 and MIT inputs;
3. required copyright, patent, trademark, and NOTICE treatment;
4. whether a separate Atlas Harness package/repository is required; and
5. the approver, date, decision, and any distribution constraints.

## Source treatment

| Source | Evidence | Allowed now | Forbidden now |
| --- | --- | --- | --- |
| Codex `4c43465` | Verified Apache-2.0 | Behavior/security evidence for the Python plan; future adaptation only after the deferred Rust activation gate | Current copying, unrecorded adaptation, or reachable unsandboxed behavior |
| OpenCode `7534d23` | Verified MIT | Selected pattern/file adaptation with retained notice | Whole-stack merge or lost attribution |
| Jcode `11cda7d` | Verified MIT | Selected pattern/file adaptation with retained notice | Unsandboxed/fail-open behavior or lost attribution |
| Claude Code public docs | Core repository observed all-rights-reserved | Neutral behavior requirements and independent implementation | Copying implementation source or implying source compatibility |
| Local Orqen `155a115` | No visible license grant | Concept-level requirements already captured in approved docs | Source, tests, prompts, schemas, or assets |
| Local documentIntelligence `ff35829` | No visible license grant | Concept-level requirements already captured in approved docs | Source, tests, prompts, schemas, or assets |

The local-tree revisions identify the Git commits reviewed. A dirty working tree
must be treated as a separate unreviewed export and cannot be a source input.

## File-level adaptation record

Before a copied or substantially adapted file enters a review, add one
`adapted_files` entry containing:

- destination repository-relative path;
- source ID from the manifest;
- original source-relative path;
- exact immutable source revision;
- source license;
- concise description of local changes; and
- accountable maintenance owner.

The verifier rejects:

- an unknown or duplicate source;
- a short or mutable Git revision;
- copying from a restricted or unverified source;
- a source/destination path that escapes its repository;
- a mismatched source revision or license;
- duplicate destination ownership; or
- `copy_allowed=true` without a verified Apache-2.0 or MIT record.

Generated files identify the generator and its source record rather than
pretending the generated output was independently authored. Vendored
dependencies use the dependency lockfile, SBOM, and package license evidence in
addition to any file-level record.

## NOTICE and attribution plan

The approved release process must:

1. ship the Atlas outbound `LICENSE` in source and binary packages;
2. ship Codex license/NOTICE content only if a future approved adaptation
   actually includes Codex-derived files;
3. retain the full MIT notice for copied OpenCode or Jcode material;
4. generate a human-readable attribution report from the manifest;
5. include dependency license inventory and SBOM checksums;
6. mark modified upstream files and summarize material local changes;
7. exclude trademarks and branding not expressly licensed; and
8. fail packaging when the manifest, lockfiles, notices, or SBOM disagree.

Attribution must survive source archives, binaries, installers, containers, and
generated SDK packages. Documentation links alone do not replace required
license text.

## Clean implementation boundary

Restricted and unlicensed sources may be used only as already-reviewed
behavioral evidence:

- requirements use neutral names and observable outcomes;
- production implementers do not copy or mechanically translate those files;
- tests assert Atlas contracts rather than source-specific implementation
  details;
- any new access to a restricted tree records purpose, revision, reviewer, and
  resulting neutral specification; and
- a written license grant triggers a new provenance review rather than silently
  changing `copy_allowed`.

Calling work “clean room” is not sufficient by itself. Legal review determines
whether separation, additional records, patent review, or non-use is required.

## Dependency and release gates

Every release candidate must provide:

- locked Python and TypeScript dependency graphs;
- Python and package-manager vulnerability/license audit results;
- SPDX-compatible dependency license inventory;
- CycloneDX or SPDX SBOM with artifact checksums;
- reproducible build evidence from a clean checkout;
- source and adapted-file provenance verification;
- bundled license/NOTICE inspection;
- known-vulnerability disposition with owner and expiry; and
- human license approval.

CI evidence can detect missing metadata and known conflicts. It cannot grant a
license, decide fair use, approve patents/trademarks, or replace counsel.

## Ownership and change control

The Python harness owner approves current production files. A future Rust owner
may approve Codex-derived files only after the separate activation gate.
Provider/event owners approve OpenCode-derived material. Session/scheduler
owners approve Jcode-derived material. The provenance owner reviews every
manifest change and all restricted-source boundaries.

Any source revision update is a reviewed change that must:

1. compare licenses and notices;
2. refresh security and privileged-path inventories;
3. map upstream changes to local adaptations;
4. rerun compatibility, audit, and provenance gates; and
5. record the update owner and merge budget.

Release is blocked while ownership, license evidence, treatment, adapted-file
mapping, required notice, or outbound approval is missing.
