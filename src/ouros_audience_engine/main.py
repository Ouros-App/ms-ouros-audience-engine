from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ouros_audience_engine.api.health import router as health_router
from ouros_audience_engine.api.internal import router as internal_router
from ouros_audience_engine.runtime import Runtime, build_runtime
from ouros_audience_engine.settings import Settings, get_settings


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    active_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_runtime = runtime or build_runtime(active_settings)
        app.state.api_services = active_runtime.api
        app.state.runtime = active_runtime
        app.state.settings = active_settings
        try:
            yield
        finally:
            if runtime is None:
                active_runtime.redis_client.close()  # type: ignore[no-untyped-call]
                active_runtime.mongo_client.close()

    app = FastAPI(
        title="Ouros Audience Engine",
        description="Internal deterministic audience membership resolver.",
        version="0.1.0",
        docs_url=None if active_settings.environment.lower() == "production" else "/docs",
        redoc_url=None,
        openapi_url=None
        if active_settings.environment.lower() == "production"
        else "/openapi.json",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(internal_router)
    if runtime is not None:
        app.state.api_services = runtime.api
        app.state.runtime = runtime
        app.state.settings = active_settings
    return app


app = create_app()
