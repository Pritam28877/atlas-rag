"""Bounded parser/retriever boundary and grounded-finish tests."""

import asyncio
import hashlib

import pytest

from app.services.harness.documents import (
    DocumentAdapterError,
    DocumentAdapterErrorCode,
    DocumentIngestPolicy,
    DocumentIngestRequest,
    DocumentSpan,
    EvidenceCandidate,
    EvidenceCitation,
    EvidenceClaim,
    EvidenceClassification,
    EvidenceClassificationRecord,
    EvidenceLedgerSnapshot,
    EvidenceQuery,
    IsolatedDocumentAdapter,
    validate_grounded_finish,
)

TENANT = "ten_" + "1" * 32
WORKSPACE = "wsp_" + "2" * 32
ARTIFACT = "art_" + "3" * 32
DOC = "doc/source"
REVISION = "b" * 64


class Reader:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = list(chunks)

    async def read(self, maximum_bytes: int) -> bytes:
        del maximum_bytes
        return self.chunks.pop(0) if self.chunks else b""


class Parser:
    def __init__(self, spans: tuple[DocumentSpan, ...]) -> None:
        self.spans = spans

    async def parse(
        self,
        request: DocumentIngestRequest,
        content: bytes,
    ) -> tuple[DocumentSpan, ...]:
        del request, content
        return self.spans

    async def close(self) -> None:
        return


class Retriever:
    async def search(self, query: EvidenceQuery) -> tuple[EvidenceCandidate, ...]:
        del query
        return ()

    async def close(self) -> None:
        return


def _request(content: bytes) -> DocumentIngestRequest:
    return DocumentIngestRequest(
        document_id=DOC,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        artifact_id=ARTIFACT,
        mime_type="text/plain",
        expected_size_bytes=len(content),
        expected_content_sha256=hashlib.sha256(content).hexdigest(),
        parser_revision="parser.v1",
    )


def test_document_adapter_bounds_stream_and_binds_revision() -> None:
    content = b"hello world"
    revision = hashlib.sha256(
        f"{ARTIFACT}:{hashlib.sha256(content).hexdigest()}:parser.v1".encode()
    ).hexdigest()
    text = "hello"
    span = DocumentSpan(
        document_id=DOC,
        revision_sha256=revision,
        page_number=1,
        start_offset=0,
        end_offset=5,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )

    async def scenario() -> None:
        adapter = IsolatedDocumentAdapter(
            DocumentIngestPolicy(allowed_mime_types=("text/plain",)),
            Parser((span,)),
            Retriever(),
        )
        version, spans = await adapter.ingest(
            _request(content), Reader((b"hello ", b"world"))
        )
        assert version.revision_sha256 == revision
        assert spans == (span,)

    asyncio.run(scenario())


def test_document_adapter_rejects_digest_and_mime_failures() -> None:
    async def scenario() -> None:
        adapter = IsolatedDocumentAdapter(
            DocumentIngestPolicy(allowed_mime_types=("text/plain",)),
            Parser(()),
            Retriever(),
        )
        with pytest.raises(DocumentAdapterError) as digest:
            await adapter.ingest(_request(b"wrong!"), Reader((b"actual",)))
        assert digest.value.code is DocumentAdapterErrorCode.DIGEST
        request = _request(b"actual").model_copy(
            update={"mime_type": "application/pdf"}
        )
        with pytest.raises(DocumentAdapterError) as mime:
            await adapter.ingest(request, Reader((b"actual",)))
        assert mime.value.code is DocumentAdapterErrorCode.MIME

    asyncio.run(scenario())


def test_grounded_finish_blocks_unknown_or_mismatched_evidence() -> None:
    query = EvidenceQuery(
        query_id="query/one",
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        text="question",
        retriever_revision="retriever.v1",
        tool_revision="tool.v1",
    )
    text = "exact"
    span = DocumentSpan(
        document_id=DOC,
        revision_sha256=REVISION,
        page_number=1,
        start_offset=0,
        end_offset=5,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    candidate = EvidenceCandidate(
        candidate_id="candidate/one",
        query_id=query.query_id,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        span=span,
        score_micros=900_000,
    )
    citation = EvidenceCitation(
        citation_id="citation/one",
        candidate_id=candidate.candidate_id,
        query_id=query.query_id,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        span=span,
        quote=text,
        quote_sha256=hashlib.sha256(text.encode()).hexdigest(),
        retriever_revision=query.retriever_revision,
        tool_revision=query.tool_revision,
    )
    snapshot = EvidenceLedgerSnapshot(
        sources=(),
        queries=(query,),
        candidates=(candidate,),
        classifications=(
            EvidenceClassificationRecord(
                candidate_id=candidate.candidate_id,
                classification=EvidenceClassification.UNKNOWN,
                reason="not classified",
            ),
        ),
        citations=(citation,),
        claims=(
            EvidenceClaim(
                claim_id="claim/one",
                text="claim",
                citation_ids=(citation.citation_id,),
            ),
        ),
    )
    decision = validate_grounded_finish(snapshot)
    assert decision.blocked_claim_ids == ("claim/one",)
