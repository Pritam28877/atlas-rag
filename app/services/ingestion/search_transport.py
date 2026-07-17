"""OpenSearch index bootstrap and bounded HTTP transport."""

import httpx

from app.core.config import SearchSettings


class SearchPublicationError(RuntimeError):
    """Retryable search dependency failure."""


class OpenSearchTransport:
    def __init__(
        self,
        settings: SearchSettings,
        dimensions: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if settings.endpoint_url is None:
            raise ValueError("search endpoint is required")
        authentication = None
        if settings.username and settings.password:
            authentication = (
                settings.username,
                settings.password.get_secret_value(),
            )
        self.target_name = settings.index_name
        self.target_version = settings.target_version
        self._dimensions = dimensions
        self._client = client or httpx.AsyncClient(
            base_url=settings.endpoint_url.rstrip("/"),
            auth=authentication,
            verify=settings.verify_tls,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
        )

    async def _ensure_index(self) -> None:
        response = await self._raw_request("GET", f"/{self.target_name}")
        if response.status_code == 200:
            mapping = response.json()[self.target_name]["mappings"]["properties"]
            dimension = mapping["embedding"].get("dimension")
            if dimension != self._dimensions:
                raise SearchPublicationError("OpenSearch vector dimension mismatch")
            missing_attempt_fields = {
                "publication_job_id",
                "publication_attempt",
            } - mapping.keys()
            if missing_attempt_fields:
                mapping_response = await self._raw_request(
                    "PUT",
                    f"/{self.target_name}/_mapping",
                    json_payload={
                        "properties": {
                            field_name: field_mapping
                            for field_name, field_mapping in {
                                "publication_job_id": {"type": "keyword"},
                                "publication_attempt": {"type": "integer"},
                            }.items()
                            if field_name in missing_attempt_fields
                        }
                    },
                )
                if mapping_response.status_code not in {200, 201}:
                    self._raise_response(mapping_response)
            return
        if response.status_code != 404:
            self._raise_response(response)
        create_response = await self._raw_request(
            "PUT",
            f"/{self.target_name}",
            json_payload={
                "settings": {"index.knn": True},
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "tenant_id": {"type": "keyword"},
                        "collection_id": {"type": "keyword"},
                        "document_version_id": {"type": "keyword"},
                        "chunk_id": {"type": "keyword"},
                        "publication_job_id": {"type": "keyword"},
                        "publication_attempt": {"type": "integer"},
                        "body": {"type": "text"},
                        "page_start": {"type": "integer"},
                        "page_end": {"type": "integer"},
                        "section_path": {"type": "keyword"},
                        "content_sha256": {"type": "keyword"},
                        "pipeline_profile": {"type": "keyword"},
                        "chunker_name": {"type": "keyword"},
                        "chunker_version": {"type": "keyword"},
                        "embedding_provider": {"type": "keyword"},
                        "embedding_model": {"type": "keyword"},
                        "embedding_model_version": {"type": "keyword"},
                        "publication_complete": {"type": "boolean"},
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": self._dimensions,
                        },
                    },
                },
            },
        )
        if create_response.status_code not in {200, 201}:
            if create_response.status_code != 400 or (
                "resource_already_exists_exception" not in create_response.text
            ):
                self._raise_response(create_response)

    async def check_connection(self) -> None:
        """Require a responsive authenticated OpenSearch cluster."""
        await self._request("GET", "/_cluster/health")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: object = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        response = await self._raw_request(
            method,
            path,
            json_payload=json_payload,
            content=content,
            headers=headers,
        )
        if response.status_code >= 400:
            self._raise_response(response)
        return response

    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        json_payload: object = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                path,
                json=json_payload,
                content=content,
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise SearchPublicationError("OpenSearch request failed") from error

    @staticmethod
    def _raise_response(response: httpx.Response) -> None:
        raise SearchPublicationError(
            f"OpenSearch returned HTTP {response.status_code}"
        )

    async def close(self) -> None:
        await self._client.aclose()
