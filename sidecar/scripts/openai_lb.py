#!/usr/bin/env python3
"""Round-robin reverse proxy for several llama-cpp OpenAI-compatible servers."""
from __future__ import annotations

import argparse
import itertools
import os

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import httpx

app = FastAPI()
_backends: list[str] = []
_cycle = None
_client: httpx.AsyncClient | None = None


def _next_backend() -> str:
    assert _cycle is not None
    return next(_cycle)


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=30.0),
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request) -> Response:
    assert _client is not None
    n = len(_backends)
    body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}
    }
    last_exc: Exception | None = None
    for _ in range(n):
        base = _next_backend().rstrip("/")
        url = f"{base}/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        try:
            resp = await _client.request(
                request.method,
                url,
                content=body,
                headers=headers,
            )
        except Exception as exc:
            last_exc = exc
            continue
        if resp.status_code in {502, 503, 504}:
            last_exc = RuntimeError(f"{base} -> {resp.status_code}")
            continue
        out_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower()
            not in {"content-encoding", "transfer-encoding", "content-length"}
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=out_headers,
            media_type=resp.headers.get("content-type"),
        )
    return JSONResponse(
        {"error": {"message": f"all backends failed: {last_exc}", "type": "unavailable_error"}},
        status_code=503,
    )


def main() -> None:
    global _backends, _cycle
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--backends",
        default=os.environ.get("LLAMA_BACKENDS", "http://127.0.0.1:8081,http://127.0.0.1:8082"),
    )
    args = parser.parse_args()
    _backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    if not _backends:
        raise SystemExit("no backends")
    _cycle = itertools.cycle(_backends)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
