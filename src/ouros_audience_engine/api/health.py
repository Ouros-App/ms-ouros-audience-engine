from fastapi import APIRouter, HTTPException, Request, status

from ouros_audience_engine.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthResponse)
def live() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=HealthResponse)
def ready(request: Request) -> HealthResponse:
    runtime = request.app.state.runtime
    try:
        runtime.metadata.check()
        runtime.redis_client.ping()
        runtime.s3_client.head_bucket(Bucket=runtime.s3_store.bucket)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="required audience storage is unavailable",
        ) from exc
    return HealthResponse(status="ok")
