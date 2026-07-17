from typing import Annotated, Never
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.api.v1.catalog_models import (
    CollectionCreate,
    CollectionPage,
    CollectionResponse,
    DocumentRegistration,
    DocumentRegistrationResponse,
    DocumentVersionStatus,
    LifecycleRequest,
    LifecycleResponse,
    UploadCompletionResponse,
)
from app.core.auth import Principal, require_principal
from app.services.catalog.errors import (
    CatalogConflictError,
    CatalogDependencyError,
    CatalogError,
    CatalogForbiddenError,
    CatalogNotFoundError,
    CatalogQuotaError,
    CatalogValidationError,
)
from app.services.catalog.service import CatalogService

router = APIRouter(tags=["document-catalog"])
AuthenticatedPrincipal = Annotated[Principal, Depends(require_principal)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=255),
]


def get_catalog_service(request: Request) -> CatalogService:
    service: CatalogService | None = getattr(request.app.state, "catalog_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "catalog unavailable"},
        )
    return service


CatalogDependency = Annotated[CatalogService, Depends(get_catalog_service)]


@router.post(
    "/collections",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_collection(
    body: CollectionCreate,
    principal: AuthenticatedPrincipal,
    service: CatalogDependency,
) -> CollectionResponse:
    try:
        row = await service.create_collection(principal, body.model_dump())
        return CollectionResponse.model_validate(row)
    except CatalogError as error:
        raise_catalog_error(error)


@router.get("/collections", response_model=CollectionPage)
async def list_collections(
    principal: AuthenticatedPrincipal,
    service: CatalogDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=1000)] = None,
) -> CollectionPage:
    try:
        rows, next_cursor = await service.list_collections(
            principal, limit, cursor
        )
        return CollectionPage(
            items=[CollectionResponse.model_validate(row) for row in rows],
            next_cursor=next_cursor,
        )
    except CatalogError as error:
        raise_catalog_error(error)


@router.get("/collections/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: UUID,
    principal: AuthenticatedPrincipal,
    service: CatalogDependency,
) -> CollectionResponse:
    try:
        row = await service.get_collection(principal, collection_id)
        return CollectionResponse.model_validate(row)
    except CatalogError as error:
        raise_catalog_error(error)


@router.post(
    "/collections/{collection_id}/documents",
    response_model=DocumentRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_document(
    collection_id: UUID,
    body: DocumentRegistration,
    idempotency_key: IdempotencyKey,
    principal: AuthenticatedPrincipal,
    service: CatalogDependency,
) -> DocumentRegistrationResponse:
    try:
        result = await service.register_document(
            principal,
            collection_id,
            idempotency_key,
            body.model_dump(exclude={"content_type"}),
        )
        return DocumentRegistrationResponse.model_validate(result)
    except CatalogError as error:
        raise_catalog_error(error)


@router.post(
    "/document-versions/{version_id}/complete-upload",
    response_model=UploadCompletionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def complete_upload(
    version_id: UUID,
    principal: AuthenticatedPrincipal,
    service: CatalogDependency,
) -> UploadCompletionResponse:
    try:
        result = await service.complete_upload(principal, version_id)
        return UploadCompletionResponse.model_validate(result)
    except CatalogError as error:
        raise_catalog_error(error)


@router.get(
    "/document-versions/{version_id}",
    response_model=DocumentVersionStatus,
)
async def get_document_version_status(
    version_id: UUID,
    principal: AuthenticatedPrincipal,
    service: CatalogDependency,
) -> DocumentVersionStatus:
    try:
        result = await service.status(principal, version_id)
        return DocumentVersionStatus.model_validate(result)
    except CatalogError as error:
        raise_catalog_error(error)


@router.post(
    "/document-versions/{version_id}/lifecycle",
    response_model=LifecycleResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_lifecycle_operation(
    version_id: UUID,
    body: LifecycleRequest,
    idempotency_key: IdempotencyKey,
    principal: AuthenticatedPrincipal,
    service: CatalogDependency,
) -> LifecycleResponse:
    try:
        result = await service.lifecycle(
            principal,
            version_id,
            body.operation,
            idempotency_key,
            body.pipeline_profile,
        )
        return LifecycleResponse.model_validate(result)
    except CatalogError as error:
        raise_catalog_error(error)


def raise_catalog_error(error: CatalogError) -> Never:
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    if isinstance(error, CatalogNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, CatalogForbiddenError):
        status_code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, CatalogConflictError):
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(error, CatalogQuotaError):
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif isinstance(error, CatalogValidationError):
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif isinstance(error, CatalogDependencyError):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = str(error) if status_code < 500 else "catalog operation failed"
    raise HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": message},
    ) from error
