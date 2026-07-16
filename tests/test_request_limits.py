import asyncio

from app.core.request_limits import RequestBodyLimitMiddleware


def test_streamed_body_without_content_length_is_bounded() -> None:
    request_messages = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]
    )
    response_messages: list[dict[str, object]] = []

    async def receive():
        return next(request_messages)

    async def send(message):
        response_messages.append(message)

    async def consume_body(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if not message.get("more_body", False):
                return

    middleware = RequestBodyLimitMiddleware(consume_body, maximum_bytes=5)
    scope = {"type": "http", "headers": []}

    asyncio.run(middleware(scope, receive, send))

    assert response_messages[0]["status"] == 413


def test_content_length_is_rejected_without_calling_application() -> None:
    application_called = False
    response_messages: list[dict[str, object]] = []

    async def application(scope, receive, send):
        nonlocal application_called
        application_called = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        response_messages.append(message)

    middleware = RequestBodyLimitMiddleware(application, maximum_bytes=5)
    scope = {"type": "http", "headers": [(b"content-length", b"6")]}

    asyncio.run(middleware(scope, receive, send))

    assert application_called is False
    assert response_messages[0]["status"] == 413
