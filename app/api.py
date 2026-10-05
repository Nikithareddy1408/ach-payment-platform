"""The HTTP API: authentication, rate limiting, request ids, metrics,
consistent error responses, and OpenAPI docs (served at /docs)."""
import hashlib
import logging
import re
import threading
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import authenticate
from .domain import STATUSES, ApiError, not_found
from .mappers import to_api_event, to_api_payment
from .payments import Actor, NewPayment
from .webhooks import to_api_endpoint

log = logging.getLogger("ach.api")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


# ── Request / response schemas (also generate the OpenAPI docs) ────────────
class CreatePaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "customerId": "C12345", "sourceAccount": "VA10001", "destinationAccount": "EXT98765", "amount": 250.00, "reference": "PMT-1001"}]})
    customerId: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    sourceAccount: str = Field(pattern=r"^[A-Za-z0-9-]{4,34}$")
    destinationAccount: str = Field(pattern=r"^[A-Za-z0-9-]{4,34}$")
    amount: int | float | str = Field(description='US dollars, at most 2 decimals: 250.00 or "250.00"')
    reference: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")

    @field_validator("amount", mode="before")
    @classmethod
    def amount_is_number_or_text(cls, value):
        # One clear message instead of one error per union type; booleans are not amounts.
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("Must be a number like 250.00")
        return value


class ErrorBody(BaseModel):
    code: str
    message: str
    details: object | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class LastError(BaseModel):
    code: str
    message: str


class PaymentOut(BaseModel):
    id: str
    customerId: str
    sourceAccount: str = Field(description="Masked: last 4 characters only")
    destinationAccount: str = Field(description="Masked: last 4 characters only")
    amount: str
    amountCents: int
    currency: str
    reference: str
    status: Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED", "RETRYING"]
    attempts: int
    bankTransferId: str | None
    lastError: LastError | None
    createdAt: str
    updatedAt: str
    completedAt: str | None


class PaymentEventOut(BaseModel):
    id: str
    sequence: int
    from_: str | None = Field(alias="from")
    to: str
    reason: str
    actor: str
    requestId: str | None
    metadata: dict
    createdAt: str


class EventsOut(BaseModel):
    paymentId: str
    events: list[PaymentEventOut]


class PaymentPage(BaseModel):
    data: list[PaymentOut]
    nextCursor: str | None


class CreateWebhookEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)
    description: str | None = Field(None, max_length=200)


class WebhookEndpointOut(BaseModel):
    id: str
    url: str
    description: str | None
    enabled: bool
    createdAt: str
    secret: str | None = Field(None, description="Returned only once, when the endpoint is created.")


ERRORS = {code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 422, 429)}


# ── Rate limiting (per API key, fixed one-minute windows) ───────────────────
class RateLimiter:
    """In-memory, per process. Behind a load balancer with many instances, use a
    shared store (e.g. Redis) or the API gateway's rate limiting instead."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._windows: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> int | None:
        """Counts a request. Returns seconds to wait if over the limit, else None."""
        window = int(time.time() // 60)
        with self._lock:
            start, count = self._windows.get(key, (window, 0))
            if start != window:
                start, count = window, 0
            count += 1
            self._windows[key] = (start, count)
        return 60 - int(time.time() % 60) if count > self.per_minute else None


def create_api(platform) -> FastAPI:
    app = FastAPI(
        title="ACH Payment Platform API",
        version="1.0.0",
        description="Submit ACH payments, track their status and full audit trail, and receive signed webhooks for every status change.",
    )
    bearer = HTTPBearer(auto_error=False, description="Your API key: sk_...")
    limiter = RateLimiter(platform.settings.rate_limit_per_minute)

    # ── Request ids + metrics for every request ──
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = incoming if _REQUEST_ID.match(incoming) else "req_" + uuid.uuid4().hex
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        route = request.scope.get("route")
        platform.metrics.http_requests.labels(
            method=request.method, route=getattr(route, "path", "unmatched"), status=str(response.status_code)
        ).observe(time.perf_counter() - start)
        return response

    # ── Authentication + rate limit for every /v1 route ──
    def current_customer(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
        header = f"Bearer {credentials.credentials}" if credentials else None
        customer_id = authenticate(platform.pool, header)
        if not customer_id:
            raise ApiError(401, "UNAUTHORIZED", "Missing or invalid API key. Send 'Authorization: Bearer sk_...'.")
        wait = limiter.hit(hashlib.sha256(header.encode()).hexdigest())
        if wait is not None:
            raise ApiError(429, "RATE_LIMITED", f"Too many requests. Retry after {wait}s.")
        return customer_id

    # ── Consistent error format everywhere ──
    def error_response(status: int, code: str, message: str, details=None, request: Request | None = None) -> JSONResponse:
        body = {"error": {"code": code, "message": message, **({"details": details} if details is not None else {})}}
        headers = {"x-request-id": request.state.request_id} if request is not None and hasattr(request.state, "request_id") else None
        return JSONResponse(status_code=status, content=body, headers=headers)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details, request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if any(e.get("type") == "json_invalid" for e in exc.errors()):
            return error_response(400, "INVALID_JSON", "Request body is not valid JSON.", request=request)
        details = []
        for e in exc.errors():
            loc = [str(p) for p in e.get("loc", ())]
            field = ".".join(loc[1:]) if loc and loc[0] in ("body", "query", "path", "header") else ".".join(loc)
            message = "Unknown field. Check the spelling." if e.get("type") == "extra_forbidden" else e.get("msg", "Invalid value")
            details.append({"field": field or "(body)", "message": message})
        return error_response(400, "VALIDATION_ERROR", "The request is invalid.", details, request)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return error_response(404, "NOT_FOUND", f"No route for {request.method} {request.url.path}", request=request)
        if exc.status_code == 405:
            return error_response(405, "METHOD_NOT_ALLOWED", f"{request.method} is not allowed here.", request=request)
        return error_response(exc.status_code, "HTTP_ERROR", str(exc.detail), request=request)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
        return error_response(500, "INTERNAL_ERROR", "Something went wrong. Quote the x-request-id header if you contact support.", request=request)

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(current_customer)], responses=ERRORS)

    # ── Payments ──
    @v1.post("/payments", tags=["Payments"], summary="Submit an ACH payment", status_code=202,
             responses={202: {"model": PaymentOut, "description": "Accepted for asynchronous processing"},
                        200: {"model": PaymentOut, "description": "Replay: same Idempotency-Key and body as an earlier request"}})
    def submit_payment(body: CreatePaymentRequest, request: Request, customer_id: str = Depends(current_customer),
                       idempotency_key: str | None = Header(None, alias="Idempotency-Key",
                                                           description="Required. Unique per payment; reuse only to retry the same request.")):
        """Validates and queues the payment, then returns immediately with status PENDING.
        Processing (the bank call, retries) happens in the background."""
        payment, replayed = platform.payments.create(
            customer_id,
            NewPayment(body.customerId, body.sourceAccount, body.destinationAccount, body.amount, body.reference),
            idempotency_key,
            Actor("api", request.state.request_id),
        )
        return JSONResponse(
            status_code=200 if replayed else 202,
            content=to_api_payment(payment),
            headers={"Idempotent-Replayed": str(replayed).lower(), "Location": f"/v1/payments/{payment['id']}"},
        )

    @v1.get("/payments", tags=["Payments"], summary="List payments (newest first)", response_model=PaymentPage)
    def list_payments(customer_id: str = Depends(current_customer),
                      status: Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED", "RETRYING"] | None = None,
                      reference: str | None = Query(None, max_length=64),
                      limit: int = Query(20, ge=1, le=100), cursor: str | None = Query(None, max_length=200)):
        rows, next_cursor = platform.payments.list(customer_id, status=status, reference=reference, limit=limit, cursor=cursor)
        return {"data": [to_api_payment(r) for r in rows], "nextCursor": next_cursor}

    @v1.get("/payments/{payment_id}", tags=["Payments"], summary="Get a payment's current status", response_model=PaymentOut)
    def get_payment(payment_id: str, customer_id: str = Depends(current_customer)):
        payment = platform.payments.get(customer_id, payment_id)
        if payment is None:
            raise not_found("PAYMENT", payment_id)
        return to_api_payment(payment)

    @v1.get("/payments/{payment_id}/events", tags=["Payments"], summary="Get a payment's complete audit trail",
            response_model=EventsOut, response_model_by_alias=True)
    def get_events(payment_id: str, customer_id: str = Depends(current_customer)):
        """Every status change, in order: what changed, why, who did it, when, and the request id for log correlation."""
        if platform.payments.get(customer_id, payment_id) is None:
            raise not_found("PAYMENT", payment_id)
        return {"paymentId": payment_id, "events": [to_api_event(e) for e in platform.payments.events(payment_id)]}

    # ── Webhooks ──
    @v1.post("/webhook-endpoints", tags=["Webhooks"], summary="Register a webhook endpoint", status_code=201,
             response_model=WebhookEndpointOut, response_model_exclude_none=True)
    def create_endpoint(body: CreateWebhookEndpointRequest, customer_id: str = Depends(current_customer)):
        """You will receive a signed POST for every payment status change. The signing secret is returned only once."""
        return to_api_endpoint(platform.webhook_endpoints.create(customer_id, body.url, body.description), include_secret=True)

    @v1.get("/webhook-endpoints", tags=["Webhooks"], summary="List webhook endpoints")
    def list_endpoints(customer_id: str = Depends(current_customer)):
        return {"data": [to_api_endpoint(e) for e in platform.webhook_endpoints.list(customer_id)]}

    @v1.delete("/webhook-endpoints/{endpoint_id}", tags=["Webhooks"], summary="Disable a webhook endpoint")
    def disable_endpoint(endpoint_id: str, customer_id: str = Depends(current_customer)):
        return to_api_endpoint(platform.webhook_endpoints.disable(customer_id, endpoint_id))

    @v1.post("/webhook-deliveries/{delivery_id}/retry", tags=["Webhooks"], summary="Re-send a webhook delivery that gave up", status_code=202)
    def retry_delivery(delivery_id: int, customer_id: str = Depends(current_customer)):
        return platform.webhook_endpoints.retry_delivery(customer_id, delivery_id)

    app.include_router(v1)

    # ── Operations (expose /metrics only on the internal network in production) ──
    @app.get("/health/live", include_in_schema=False)
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready():
        try:
            with platform.pool.connection() as conn:
                conn.execute("SELECT 1")
            return {"status": "ok", "database": "ok"}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable"})

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(platform.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app
