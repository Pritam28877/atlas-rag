import asyncio
import json
from uuid import UUID

import httpx
import pytest

from app.core.config import SearchSettings
from app.services.ingestion.chunking import ChunkRecord
from app.services.ingestion.embedding_artifacts import EmbeddedChunk
from app.services.ingestion.search_adapter import (
    OpenSearchIndexAdapter,
    SearchPublicationError,
    SearchRecord,
)

TENANT_ID = UUID("00000000-0000-0000-0000-000000000071")
COLLECTION_ID = UUID("00000000-0000-0000-0000-000000000072")
VERSION_ID = UUID("00000000-0000-0000-0000-000000000073")
JOB_ID = UUID("00000000-0000-0000-0000-000000000076")


def test_opensearch_publication_enforces_scope_and_stages_visibility() -> None:
    asyncio.run(_opensearch_publication_case())


async def _opensearch_publication_case() -> None:
    indexed: dict[str, dict[str, object]] = {}
    activation_seen = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal activation_seen
        if request.method == "GET":
            return httpx.Response(404, request=request)
        if request.method == "PUT":
            return httpx.Response(200, json={"acknowledged": True}, request=request)
        if request.url.path == "/_bulk":
            lines = request.content.decode().splitlines()
            for index in range(0, len(lines), 2):
                identity = json.loads(lines[index])["index"]["_id"]
                indexed[identity] = json.loads(lines[index + 1])
            return httpx.Response(
                200, json={"errors": False, "items": []}, request=request
            )
        if request.url.path.endswith("/_mget"):
            identities = json.loads(request.content)["ids"]
            documents = [
                {
                    "_id": identity,
                    "found": True,
                    "_source": indexed[identity],
                }
                for identity in identities
            ]
            return httpx.Response(200, json={"docs": documents}, request=request)
        if request.url.path.endswith("/_update_by_query"):
            activation_seen = True
            return httpx.Response(
                200,
                json={
                    "version_conflicts": 0,
                    "failures": [],
                    "total": 1,
                    "updated": 1,
                },
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://search.test"
    )
    adapter = OpenSearchIndexAdapter(
        SearchSettings(endpoint_url="https://search.test"),
        dimensions=2,
        client=client,
    )
    chunk = ChunkRecord(
        chunk_id=UUID("00000000-0000-0000-0000-000000000074"),
        ordinal=0,
        page_start=1,
        page_end=1,
        char_start=0,
        char_end=4,
        text="text",
        content_sha256="0" * 64,
        token_count=1,
    )
    record = SearchRecord(
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        document_version_id=VERSION_ID,
        chunk=chunk,
        publication_job_id=JOB_ID,
        publication_attempt=1,
        embedding=EmbeddedChunk(
            chunk_id=chunk.chunk_id,
            vector=(0.5, 0.5),
            checksum_sha256="1" * 64,
        ),
    )

    try:
        publications = await adapter.publish([record])
        await adapter.activate_version(
            TENANT_ID,
            COLLECTION_ID,
            VERSION_ID,
            1,
            publication_job_id=JOB_ID,
            publication_attempt=1,
        )
        indexed_record = indexed[f"{JOB_ID}:1:{chunk.chunk_id}"]

        assert len(publications) == 2
        assert indexed_record["tenant_id"] == str(TENANT_ID)
        assert indexed_record["collection_id"] == str(COLLECTION_ID)
        assert indexed_record["document_version_id"] == str(VERSION_ID)
        assert indexed_record["publication_job_id"] == str(JOB_ID)
        assert indexed_record["publication_attempt"] == 1
        assert indexed_record["publication_complete"] is False
        assert activation_seen is True
    finally:
        await adapter.close()


def test_opensearch_bulk_partial_failure_is_retryable() -> None:
    asyncio.run(_opensearch_partial_failure_case())


def test_opensearch_delete_is_exactly_tenant_version_scoped() -> None:
    asyncio.run(_opensearch_delete_scope_case())


def test_opensearch_delete_timeout_is_retryable() -> None:
    async def run_case() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "deleted": 1,
                    "timed_out": True,
                    "version_conflicts": 0,
                    "failures": [],
                },
                request=request,
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://search.test",
        )
        adapter = OpenSearchIndexAdapter(
            SearchSettings(endpoint_url="https://search.test"),
            dimensions=2,
            client=client,
        )
        try:
            with pytest.raises(SearchPublicationError, match="incomplete"):
                await adapter.delete_version(TENANT_ID, COLLECTION_ID, VERSION_ID)
        finally:
            await adapter.close()

    asyncio.run(run_case())


def test_opensearch_index_connection_failure_is_retryable() -> None:
    async def run_case() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("search unavailable", request=request)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://search.test",
        )
        adapter = OpenSearchIndexAdapter(
            SearchSettings(endpoint_url="https://search.test"),
            dimensions=2,
            client=client,
        )
        try:
            with pytest.raises(SearchPublicationError, match="request failed"):
                await adapter._ensure_index()
        finally:
            await adapter.close()

    asyncio.run(run_case())


async def _opensearch_delete_scope_case() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"deleted": 2, "version_conflicts": 0, "failures": []},
            request=request,
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://search.test"
    )
    adapter = OpenSearchIndexAdapter(
        SearchSettings(endpoint_url="https://search.test"),
        dimensions=2,
        client=client,
    )
    try:
        deleted = await adapter.delete_version(TENANT_ID, COLLECTION_ID, VERSION_ID)
        assert deleted == 2
        assert captured == {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": str(TENANT_ID)}},
                        {"term": {"collection_id": str(COLLECTION_ID)}},
                        {"term": {"document_version_id": str(VERSION_ID)}},
                    ]
                }
            }
        }
    finally:
        await adapter.close()


async def _opensearch_partial_failure_case() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, request=request)
        if request.method == "PUT":
            return httpx.Response(200, json={"acknowledged": True}, request=request)
        return httpx.Response(
            200,
            json={"errors": True, "items": [{"index": {"status": 429}}]},
            request=request,
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://search.test"
    )
    adapter = OpenSearchIndexAdapter(
        SearchSettings(endpoint_url="https://search.test"),
        dimensions=2,
        client=client,
    )
    chunk = ChunkRecord(
        chunk_id=UUID("00000000-0000-0000-0000-000000000075"),
        ordinal=0,
        page_start=1,
        page_end=1,
        char_start=0,
        char_end=4,
        text="text",
        content_sha256="2" * 64,
        token_count=1,
    )
    record = SearchRecord(
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        document_version_id=VERSION_ID,
        chunk=chunk,
        publication_job_id=JOB_ID,
        publication_attempt=1,
        embedding=EmbeddedChunk(
            chunk_id=chunk.chunk_id,
            vector=(0.5, 0.5),
            checksum_sha256="3" * 64,
        ),
    )

    try:
        with pytest.raises(SearchPublicationError, match="incomplete"):
            await adapter.publish([record])
    finally:
        await adapter.close()
