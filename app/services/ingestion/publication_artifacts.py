"""Index publication manifest helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

from app.services.ingestion.publication_models import PublicationRecord


class PublicationManifestWriter:
    def __init__(
        self,
        path: Path,
        tenant_id: UUID,
        document_version_id: UUID,
        target_name: str,
        target_version: str,
    ) -> None:
        self._path = path
        self._tenant_id = tenant_id
        self._document_version_id = document_version_id
        self._output = path.open("x", encoding="utf-8")
        self._count = 0
        self._write(
            {
                "record_type": "header",
                "schema_version": "index-manifest-v1",
                "tenant_id": str(tenant_id),
                "document_version_id": str(document_version_id),
                "target_name": target_name,
                "target_version": target_version,
            }
        )

    def write(self, publication: PublicationRecord) -> None:
        self._write(
            {
                "record_type": "publication",
                "tenant_id": str(self._tenant_id),
                "document_version_id": str(self._document_version_id),
                "publication": {
                    "chunk_id": str(publication.chunk_id),
                    "publication_kind": publication.publication_kind,
                    "external_record_id": publication.external_record_id,
                },
            }
        )
        self._count += 1

    def finish(self) -> int:
        self._write({"record_type": "manifest", "publication_count": self._count})
        self._output.flush()
        self._output.close()
        return self._count

    def close(self) -> None:
        if not self._output.closed:
            self._output.close()

    def _write(self, record: dict[str, object]) -> None:
        json.dump(record, self._output, ensure_ascii=False, separators=(",", ":"))
        self._output.write("\n")


def iter_publication_manifest(
    path: Path,
    tenant_id: UUID,
    document_version_id: UUID,
) -> Iterator[PublicationRecord]:
    count = 0
    declared_count: int | None = None
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            record = json.loads(line)
            if line_number == 1 and record.get("record_type") != "header":
                raise ValueError("index manifest header is missing")
            if record.get("tenant_id") not in {None, str(tenant_id)}:
                raise ValueError("index manifest tenant does not match")
            if record.get("document_version_id") not in {
                None,
                str(document_version_id),
            }:
                raise ValueError("index manifest version does not match")
            if record.get("record_type") == "publication":
                publication = record.get("publication")
                if not isinstance(publication, dict):
                    raise ValueError("index publication record is invalid")
                count += 1
                yield PublicationRecord(
                    chunk_id=UUID(str(publication["chunk_id"])),
                    publication_kind=str(publication["publication_kind"]),
                    external_record_id=str(publication["external_record_id"]),
                )
            elif record.get("record_type") == "manifest":
                declared_count = int(record.get("publication_count", -1))
    if declared_count != count:
        raise ValueError("index manifest count does not match records")
