import asyncio
import logging
import mimetypes
import os
from pathlib import Path

from api_analytics.fastapi import Analytics
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi_simple_cachecontrol.middleware import CacheControlMiddleware
from fastapi_simple_cachecontrol.types import CacheControl
from huggingface_hub.hf_api import LocalTokenNotFoundError
from starlette import status
from starlette.responses import JSONResponse

from api import websocket_manager
from api.routes import (
    general,
    generate,
    hardware,
    models,
    outputs,
    settings,
    static,
    test,
    ws,
)
from api.websockets.data import Data
from api.websockets.notification import Notification
from core import shared
from core.types import InferenceBackend

logger = logging.getLogger(__name__)


async def log_request(request: Request):
    "Log all requests"

    logger.debug(
        f"url: {request.url}"
        # f"url: {request.url}, params: {request.query_params}, body: {await request.body()}"
    )


app = FastAPI(
    docs_url="/api/docs", redoc_url="/api/redoc", dependencies=[Depends(log_request)]
)

mimetypes.init()
mimetypes.add_type("application/javascript", ".js")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    "Output validation errors into debug log for debugging purposes"

    logger.debug(exc)

    try:
        why = str(exc).split(":")[1].strip()
        await websocket_manager.broadcast(
            data=Notification(
                severity="error",
                message=f"Validation error: {why}",
                title="Validation Error",
            )
        )
    except IndexError:
        logger.debug("Unable to parse validation error, skipping the error broadcast")

    content = {
        "status_code": 10422,
        "message": f"{exc}".replace("\n", " ").replace("   ", " "),
        "data": None,
    }
    return JSONResponse(
        content=content, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
    )


@app.exception_handler(LocalTokenNotFoundError)
async def hf_token_error(_request, _exc):
    await websocket_manager.broadcast(
        data=Data(
            data_type="token",
            data={"huggingface": "missing"},
        )
    )

    return JSONResponse(
        content={
            "status_code": 10422,
            "message": "HuggingFace token not found",
            "data": None,
        },
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@app.exception_handler(404)
async def custom_http_exception_handler(_request, _exc):
    "Redirect back to the main page (frontend will handle it)"

    return FileResponse("frontend/dist/index.html")


@app.on_event("startup")
async def startup_event():
    "Prepare the event loop for other asynchronous tasks"

    # Inject the logger
    from rich.logging import RichHandler

    # Disable duplicate logger
    logging.getLogger("uvicorn").handlers = []

    for logger_ in ("uvicorn.access", "uvicorn.error", "fastapi"):
        l = logging.getLogger(logger_)
        handler = RichHandler(
            rich_tracebacks=True, show_time=False, omit_repeated_times=False
        )
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(name)s » %(message)s", datefmt="%H:%M:%S"
            )
        )
        l.handlers = [handler]

    if logger.level > logging.DEBUG:
        from transformers import logging as transformers_logging

        transformers_logging.set_verbosity_error()

    shared.asyncio_loop = asyncio.get_event_loop()
    websocket_manager.loop = shared.asyncio_loop

    perf_task = asyncio.create_task(websocket_manager.perf_loop())
    shared.asyncio_tasks.append(perf_task)

    from core.config import config

    if config.api.autoloaded_models:
        from core.shared_dependent import cached_model_list, gpu

        all_models = cached_model_list.all()

        for model in config.api.autoloaded_models:
            if model in [i.path for i in all_models]:
                backend: InferenceBackend = [i.backend for i in all_models if i.path == model][0]  # type: ignore
                await gpu.load_model(model, backend)
            else:
                logger.warning(f"Autoloaded model {model} not found, skipping")

    logger.info("Started WebSocketManager performance monitoring loop")
    logger.info(f"UI Available at: http://localhost:{shared.api_port}/")


@app.on_event("shutdown")
async def shutdown_event():
    "Close all WebSocket connections"

    logger.info("Closing all WebSocket connections")
    await websocket_manager.close_all()


# Enable FastAPI Analytics if key is provided
key = os.getenv("FASTAPI_ANALYTICS_KEY")
if key:
    app.add_middleware(Analytics, api_key=key)
    logger.info("Enabled FastAPI Analytics")
else:
    logger.debug("No FastAPI Analytics key provided, skipping")

# Mount routers
## HTTP
app.include_router(static.router)

# Walk the routes folder and mount all routers
for file in Path("api/routes").iterdir():
    if file.is_file():
        if (
            file.name != "__init__.py"
            and file.suffix == ".py"
            and file.stem != "static"
        ):
            logger.debug(f"Mounting: {file} as /api/{file.stem}")
            module = __import__(f"api.routes.{file.stem}", fromlist=["router"])
            app.include_router(module.router, prefix=f"/api/{file.stem}")

## WebSockets
app.include_router(ws.router, prefix="/api/websockets")

# Mount outputs folder
output_folder = Path("data/outputs")
output_folder.mkdir(exist_ok=True)
app.mount("/data/outputs", StaticFiles(directory="data/outputs"), name="outputs")

# Mount static files (css, js, images, etc.)
static_app = FastAPI()
static_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
static_app.add_middleware(
    CacheControlMiddleware, cache_control=CacheControl("no-cache")
)
static_app.mount("/", StaticFiles(directory="frontend/dist/assets"), name="assets")

app.mount("/assets", static_app)

# Allow CORS for specified origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
