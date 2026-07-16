from collections.abc import Awaitable, Callable

from starlette.types import Message, Receive, Scope, Send

AsgiApplication = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before framework request parsing buffers them."""

    def __init__(self, app: AsgiApplication, maximum_bytes: int) -> None:
        self._app = app
        self._maximum_bytes = maximum_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        content_length = _content_length(scope)
        if content_length is not None and content_length > self._maximum_bytes:
            await _send_too_large(send)
            return

        received_bytes = 0
        rejected = False

        async def bounded_receive() -> Message:
            nonlocal received_bytes, rejected
            message = await receive()
            if message["type"] != "http.request":
                return message
            body = message.get("body", b"")
            received_bytes += len(body)
            if received_bytes > self._maximum_bytes:
                rejected = True
                return {"type": "http.disconnect"}
            return message

        response_started = False

        async def bounded_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, bounded_receive, bounded_send)
        except Exception:
            if not rejected:
                raise
        if rejected and not response_started:
            await _send_too_large(send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_too_large(send: Send) -> None:
    body = b'{"detail":"request body too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
