"""Authenticated localhost DMS detail broker; never performs approval or SQL execution."""

import asyncio
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.dms_api import DmsCliReader, dms_error, order_id, validate_snapshot
from app.dms_client import read_dms_token
from app.errors import AppError

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class DmsHostConfig:
    token_file: Path
    binary: str = ""
    profile: str = ""
    tenant_id: str = ""
    port: int = 8182
    timeout: float = 50

    @classmethod
    def from_environment(cls):
        return cls(
            token_file=Path(os.environ.get("DMS_DETAILS_TOKEN_FILE", str(ROOT / ".data/dms-token"))),
            binary=os.environ.get("DMS_ALIYUN_BINARY", ""),
            profile=os.environ.get("DMS_ALIYUN_PROFILE", ""),
            tenant_id=os.environ.get("DMS_TENANT_ID", ""),
            port=int(os.environ.get("DMS_DETAILS_PORT", "8182")),
        )


def create_dms_host_app(config=None, *, reader=None):
    config = config or DmsHostConfig.from_environment()
    reader = reader or DmsCliReader(
        binary=config.binary, profile=config.profile,
        tenant_id=config.tenant_id, timeout=config.timeout,
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lock = asyncio.Lock()

    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.get("/healthz")
    async def health():
        try:
            read_dms_token(config.token_file)
            ready = bool(getattr(reader, "binary", ""))
        except AppError:
            ready = False
        return {"status": "ok", "ready": ready, "readiness_scope": "local_runtime"}

    @app.post("/details")
    async def details(request: Request):
        expected = f"Bearer {read_dms_token(config.token_file)}"
        actual = request.headers.get("authorization", "")
        if len(actual) > 256 or not hmac.compare_digest(actual.encode(), expected.encode()):
            return JSONResponse({"code": "unauthorized"}, status_code=401)
        if lock.locked():
            raise dms_error("dms_details_busy", 429)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return JSONResponse({"code": "invalid_content_type"}, status_code=415)
        async with lock:
            data = bytearray()
            try:
                async with asyncio.timeout(5):
                    async for chunk in request.stream():
                        data.extend(chunk)
                        if len(data) > 1024:
                            return JSONResponse({"code": "payload_too_large"}, status_code=413)
                payload = json.loads(data)
                if not isinstance(payload, dict) or payload.keys() != {"order_id"}:
                    raise ValueError("Invalid input")
                identifier = order_id(payload["order_id"])
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return JSONResponse({"code": "invalid_payload"}, status_code=422)
            except TimeoutError:
                return JSONResponse({"code": "request_timeout"}, status_code=408)
            result = await asyncio.to_thread(reader.fetch, identifier)
            return validate_snapshot(result, identifier)

    return app


def main():
    import uvicorn

    config = DmsHostConfig.from_environment()
    read_dms_token(config.token_file)
    if not 1024 <= config.port <= 65535:
        raise ValueError("Invalid port")
    uvicorn.run(
        create_dms_host_app(config), host="127.0.0.1", port=config.port,
        access_log=False, log_level="warning",
    )


if __name__ == "__main__":
    try:
        main()
    except (AppError, OSError, ValueError):
        raise SystemExit("DMS 详情服务启动失败，请检查本机令牌、CLI 和端口配置。") from None
