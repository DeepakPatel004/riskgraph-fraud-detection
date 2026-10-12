"""Scoring/explorer/dashboard envelope; legacy endpoint responses stay unchanged."""

from fastapi import HTTPException
from fastapi.exception_handlers import (
    http_exception_handler, request_validation_exception_handler,
)
from fastapi.responses import JSONResponse


class ScoringAPIError(HTTPException):
    """Messages/codes are application constants, never dependency exception text."""

    def __init__(self, status_code, detail, *, code, retryable=False, headers=None):
        super().__init__(status_code, detail, headers)
        self.code = code
        self.retryable = retryable


def is_scoring_request(request):
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path) == "/api/v1/transactions/score"


def is_graph_explorer_request(request):
    return getattr(request.scope.get("route"), "path", None) == "/api/v1/transactions/{transaction_id}/graph"


def is_dashboard_request(request):
    return getattr(request.scope.get("route"), "path", None) in {
        "/api/v1/dashboard/summary", "/api/v1/dashboard/risk-distribution",
        "/api/v1/dashboard/transaction-trends", "/api/v1/dashboard/alert-status",
    }


async def scoring_http_error(request, error):
    explorer = is_graph_explorer_request(request)
    dashboard = is_dashboard_request(request)
    if not (is_scoring_request(request) or explorer or dashboard):
        return await http_exception_handler(request, error)
    if isinstance(error, ScoringAPIError):
        detail, code, retryable, headers = error.detail, error.code, error.retryable, error.headers
    else:
        # Even an injected dependency's HTTPException can contain private data.
        detail, code, retryable = {
            409: ("Conflicting scoring request", "REQUEST_CONFLICT", False),
            422: ("Invalid scoring request", "VALIDATION_ERROR", False),
            503: ("Scoring service is unavailable", "SCORING_UNAVAILABLE", False),
        }.get(error.status_code, ("Scoring request failed", "SCORING_FAILED", False))
        headers = None
        if explorer:
            detail, code, retryable = "Graph explorer request failed", "GRAPH_EXPLORER_FAILED", False
        if dashboard:
            detail, code, retryable = "Dashboard request failed", "DASHBOARD_FAILED", False
    return JSONResponse(status_code=error.status_code, headers=headers, content={
        "detail": detail, "error": {"code": code, "retryable": retryable},
    })


async def scoring_validation_error(request, error):
    if not (is_scoring_request(request) or is_graph_explorer_request(request) or is_dashboard_request(request)):
        return await request_validation_exception_handler(request, error)
    return JSONResponse(status_code=422, content={
        # Omit input and exception context, which may contain payment tokens.
        "detail": [{"loc": list(item["loc"]), "type": item["type"], "msg": item["msg"]}
                   for item in error.errors()],
        "error": {"code": "VALIDATION_ERROR", "retryable": False},
    })
