# Independent review and bounded soak

P32.2 is an execution gate, not a claim made from local tests. The release
candidate remains blocked until an independent security reviewer, license
reviewer, and platform operator sign the same evidence bundle.

## Review packet

Record the candidate revision, dependency lockfile digests, SBOM digest,
provenance manifest digest, Python version, kernel/sandbox capabilities, and
the exact commands used. Reviewers must inspect:

- privileged reachability and every degraded platform decision;
- prompt, credential, cookie, header, and PII redaction before export;
- unknown-event preservation and writer-gate rejection during rollback;
- package notices, source licenses, and outbound-license approval;
- queue, retry, process, file, socket, and temporary-directory cleanup.

Each finding records severity, file/line or evidence reference, owner, due
date, disposition, and a rerun command. Critical/high findings or an unsigned
review keep the release blocked.

## 24-hour soak packet

Run only in an explicit disposable environment with cloud calls disabled unless
the provider matrix separately authorizes a capped test. Sample at least every
minute and retain raw samples plus five-minute summaries for:

The `SoakAccumulator` contract keeps only counters and maxima in process memory;
raw samples belong in the bounded evidence store. A violation remains visible
in the summary and makes `complete` false.

| Signal | Bound to prove |
| --- | --- |
| RSS and child processes | No monotonic growth after warm-up |
| Event/tool/provider queues | Never exceed configured caps; drops are counted |
| SQLite/database and artifact bytes | Growth stays within the declared quota and retention policy |
| Retries, subscribers, sockets, and temporary files | Resources return to baseline after cancellation |
| Cost, token, and request counters | Stay below the candidate budget |

The packet must include start/end UTC, sample count, maximum and p95 values,
failure injections, restart/recovery results, and operator signature. No soak
has been executed for this candidate yet; this is an explicit blocker.
