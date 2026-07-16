class CatalogError(Exception):
    code = "CATALOG_ERROR"


class CatalogNotFoundError(CatalogError):
    code = "NOT_FOUND"


class CatalogForbiddenError(CatalogError):
    code = "FORBIDDEN"


class CatalogConflictError(CatalogError):
    code = "CONFLICT"


class CatalogQuotaError(CatalogError):
    code = "QUOTA_EXCEEDED"


class CatalogValidationError(CatalogError):
    code = "INVALID_UPLOAD"


class CatalogDependencyError(CatalogError):
    code = "DEPENDENCY_UNAVAILABLE"
