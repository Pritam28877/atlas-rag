from enum import StrEnum


class CollectionRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class VersionState(StrEnum):
    RECEIVED = "received"
    VALIDATING = "validating"
    QUEUED = "queued"
    PARSING = "parsing"
    OCR = "ocr"
    NORMALIZING = "normalizing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    READY = "ready"
    READY_WITH_WARNINGS = "ready_with_warnings"
    DEDUPLICATED = "deduplicated"
    REJECTED = "rejected"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    DELETED = "deleted"


class IngestionStage(StrEnum):
    INTAKE = "intake"
    PREFLIGHT = "preflight"
    NATIVE_PARSE = "native_parse"
    OCR = "ocr"
    NORMALIZE = "normalize"
    CHUNK = "chunk"
    EMBED = "embed"
    INDEX = "index"
    DELETE = "delete"
    REPROCESS = "reprocess"


class JobState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"


class ArtifactType(StrEnum):
    ORIGINAL_PDF = "original_pdf"
    INSPECTION_REPORT = "inspection_report"
    EXTRACTED_PAGES = "extracted_pages"
    LAYOUT_BLOCKS = "layout_blocks"
    OCR_RESULT = "ocr_result"
    NORMALIZED_DOCUMENT = "normalized_document"
    CHUNK_MANIFEST = "chunk_manifest"
    INDEX_MANIFEST = "index_manifest"


class PublicationKind(StrEnum):
    LEXICAL = "lexical"
    VECTOR = "vector"
