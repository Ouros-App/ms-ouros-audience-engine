from fastapi import FastAPI

from ouros_audience_engine.api.health import router as health_router
from ouros_audience_engine.api.internal import ApiServices
from ouros_audience_engine.api.internal import router as internal_router
from ouros_audience_engine.runtime import Runtime, build_runtime
from ouros_audience_engine.settings import Settings, get_settings


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    active_runtime = runtime or build_runtime(active_settings)
    app = FastAPI(
        title="Ouros Audience Engine",
        description="Internal deterministic audience membership resolver.",
        version="0.1.0",
        docs_url=None if active_settings.environment.lower() == "production" else "/docs",
        redoc_url=None,
        openapi_url=None
        if active_settings.environment.lower() == "production"
        else "/openapi.json",
    )
    app.state.api_services = ApiServices(
        active_runtime.metadata,
        active_runtime.bitmap_reader,
        active_runtime.config,
        active_settings.service_token,
    )
    app.state.runtime = active_runtime
    app.include_router(health_router)
    app.include_router(internal_router)
    return app


app = create_app()
