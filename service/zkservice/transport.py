"""Bound JSON requests and use one error envelope across the HTTP API."""

from collections.abc import Mapping

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.headers = headers


def error_response(error: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={"error": {"code": error.code, "message": str(error)}},
        headers=error.headers,
    )


class RequestPolicy:
    """Enforce the byte limit before Starlette or Pydantic reads a JSON body."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        if scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send_response)
            return

        try:
            length = int(Headers(scope=scope).get("content-length", "0"))
            if length < 0:
                raise ValueError
            if length > self.max_body_bytes:
                raise self._too_large()

            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > self.max_body_bytes:
                    raise self._too_large()
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
        except ValueError:
            response = error_response(ApiError(400, "invalid_content_length", "Invalid Content-Length"))
            await response(scope, receive, send_response)
            return
        except ApiError as error:
            await error_response(error)(scope, receive, send_response)
            return

        body_delivered = False

        async def receive_body() -> Message:
            nonlocal body_delivered
            if body_delivered:
                return await receive()
            body_delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, receive_body, send_response)

    def _too_large(self) -> ApiError:
        return ApiError(413, "request_too_large", f"Request body exceeds {self.max_body_bytes} bytes")
