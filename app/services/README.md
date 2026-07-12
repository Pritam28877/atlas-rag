# Services

Add a service package only when a RAG capability is implemented. Typical
boundaries are `ingestion`, `retrieval`, and `generation`. Keep FastAPI,
request, and response concerns in `app/api`; services receive validated domain
values and return domain results.

