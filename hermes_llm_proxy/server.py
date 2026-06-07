from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from aiohttp import web


HERMES_SRC = os.environ.get("HERMES_SRC", "/opt/hermes")
if HERMES_SRC and HERMES_SRC not in sys.path:
    sys.path.insert(0, HERMES_SRC)

os.environ.setdefault("HERMES_HOME", "/opt/data")
os.environ.setdefault("HOME", "/opt/data/home")

logger = logging.getLogger("hermes_llm_proxy")

MAX_REQUEST_BYTES = int(os.environ.get("HERMES_LLM_PROXY_MAX_REQUEST_BYTES", "20000000"))


def _load_hermes_env() -> None:
    try:
        from hermes_cli.env_loader import load_hermes_dotenv  # type: ignore
        from hermes_constants import get_hermes_home  # type: ignore

        hermes_home = get_hermes_home()
        project_env = Path(HERMES_SRC) / ".env"
        load_hermes_dotenv(hermes_home=hermes_home, project_env=project_env)
    except Exception as exc:
        logger.debug("Hermes dotenv load skipped: %s", exc)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return str(value)


def _sse_payload(data: Any) -> bytes:
    return ("data: " + json.dumps(_jsonable(data), ensure_ascii=False, default=str) + "\n\n").encode("utf-8")


def _openai_error(message: str, status: int = 400, code: Optional[str] = None) -> web.Response:
    return web.json_response(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error" if status < 500 else "server_error",
                "code": code,
            }
        },
        status=status,
    )


class HermesLLMProxy:
    def __init__(self) -> None:
        _load_hermes_env()
        self.host = os.environ.get("HERMES_LLM_PROXY_HOST", "0.0.0.0")
        self.port = int(os.environ.get("HERMES_LLM_PROXY_PORT", "8766"))
        self.api_key = os.environ.get("HERMES_LLM_PROXY_API_KEY", "")

    def _check_auth(self, request: web.Request) -> Optional[web.Response]:
        if not self.api_key:
            return None
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        token = token or request.headers.get("X-API-Key", "").strip()
        if token != self.api_key:
            return web.json_response(
                {"error": {"message": "Invalid or missing API key", "type": "authentication_error"}},
                status=401,
            )
        return None

    def _resolve_client(self, body: Dict[str, Any]) -> Tuple[Any, str, str]:
        from agent.auxiliary_client import resolve_provider_client  # type: ignore
        from gateway.run import _resolve_gateway_model  # type: ignore
        from hermes_cli.runtime_provider import resolve_runtime_provider  # type: ignore

        runtime = resolve_runtime_provider()
        provider = str(runtime.get("provider") or "auto")
        requested_model = str(body.get("model") or "").strip()
        model = requested_model or _resolve_gateway_model()
        api_mode = runtime.get("api_mode")

        client, resolved_model = resolve_provider_client(
            provider,
            model=model,
            raw_codex=True,
            explicit_base_url=runtime.get("base_url"),
            explicit_api_key=runtime.get("api_key"),
            api_mode=api_mode,
        )
        if client is None:
            raise RuntimeError(f"Could not resolve Hermes provider credentials for provider '{provider}'")
        final_model = requested_model or resolved_model or model
        if not hasattr(client, "responses"):
            raise RuntimeError(
                f"Resolved provider '{provider}' does not expose OpenAI Responses API directly"
            )
        return client, final_model, provider

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "platform": "hermes-llm-proxy",
                "mode": "llm-auth-responses-proxy",
            }
        )

    async def handle_models(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            from gateway.run import _resolve_gateway_model  # type: ignore

            model = _resolve_gateway_model() or "hermes-configured-model"
        except Exception:
            model = "hermes-configured-model"
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": 0,
                        "owned_by": "hermes-auth",
                    }
                ],
            }
        )

    async def handle_capabilities(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        return web.json_response(
            {
                "object": "capabilities",
                "platform": "hermes-llm-proxy",
                "responses": {
                    "create": True,
                    "stream": True,
                    "passthrough": True,
                },
                "hermes_runtime": {
                    "auth_only": True,
                    "agent_loop": False,
                    "sessions": False,
                    "memory": False,
                    "tools": False,
                    "soul": False,
                },
            }
        )

    async def handle_responses(self, request: web.Request) -> web.StreamResponse:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
        except Exception:
            return _openai_error("Invalid JSON in request body")

        if not isinstance(body, dict):
            return _openai_error("Request body must be a JSON object")

        stream = bool(body.get("stream"))
        try:
            client, model, provider = self._resolve_client(body)
        except Exception as exc:
            logger.exception("Failed to resolve Hermes LLM client")
            return _openai_error(str(exc), status=500)

        payload = dict(body)
        payload.setdefault("model", model)

        if stream:
            return await self._handle_streaming_response(request, client, payload, provider)

        loop = asyncio.get_running_loop()

        def _call() -> Any:
            try:
                return client.responses.create(**payload)
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

        try:
            result = await loop.run_in_executor(None, _call)
        except Exception as exc:
            logger.exception("LLM Responses API request failed")
            return _openai_error(str(exc), status=502)
        return web.json_response(_jsonable(result))

    async def _handle_streaming_response(
        self,
        request: web.Request,
        client: Any,
        payload: Dict[str, Any],
        provider: str,
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Hermes-Provider": provider,
            },
        )

        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _consume() -> None:
            try:
                stream = client.responses.create(**payload)
                for event in stream:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, {"error": str(exc)})
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, _consume)
        await response.prepare(request)
        while True:
            item = await queue.get()
            if item is None:
                break
            await response.write(_sse_payload(item))
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response


@web.middleware
async def body_limit_middleware(request: web.Request, handler: Callable[[web.Request], Any]) -> web.Response:
    if request.method in {"POST", "PUT", "PATCH"}:
        content_length = request.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    return _openai_error("Request body too large", status=413)
            except ValueError:
                pass
    return await handler(request)


def create_app() -> web.Application:
    proxy = HermesLLMProxy()
    app = web.Application(client_max_size=MAX_REQUEST_BYTES, middlewares=[body_limit_middleware])
    app["proxy"] = proxy
    app.router.add_get("/health", proxy.handle_health)
    app.router.add_get("/v1/models", proxy.handle_models)
    app.router.add_get("/v1/capabilities", proxy.handle_capabilities)
    app.router.add_post("/v1/responses", proxy.handle_responses)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("HERMES_LLM_PROXY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    proxy: HermesLLMProxy = app["proxy"]
    web.run_app(app, host=proxy.host, port=proxy.port)


if __name__ == "__main__":
    main()
