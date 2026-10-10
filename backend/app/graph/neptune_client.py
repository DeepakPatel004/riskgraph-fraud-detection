"""Neptune Database HTTPS openCypher transport; construction makes no requests."""

import json
import math
import re
from typing import Protocol
from urllib.parse import urlencode

import httpx


class NeptuneError(RuntimeError):
    """Safe error text deliberately excludes queries, tokens and response bodies."""


class NeptuneHTTPError(NeptuneError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Neptune returned HTTP {status_code}")


class NeptuneResponseError(NeptuneError):
    pass


class NeptuneTransportError(NeptuneError):
    pass


class RequestSigner(Protocol):
    def sign(self, method: str, url: str, body: bytes, headers: dict[str, str]) -> dict[str, str]:
        ...


class SigV4Signer:
    """Resolve refreshable credentials on each request via botocore's standard chain.

    Imports and credential lookup are lazy. Optional session injection is useful
    for tests; no secret material is configured in application settings.
    """

    def __init__(self, region: str, session=None):
        self.region = region
        self._session = session

    def sign(self, method: str, url: str, body: bytes, headers: dict[str, str]) -> dict[str, str]:
        try:
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
            from botocore.session import get_session
        except ImportError:
            raise NeptuneError("IAM signing requires optional requirements-neptune.txt") from None
        try:
            if self._session is None:
                self._session = get_session()
            credentials = self._session.get_credentials()
            if credentials is None:
                raise NeptuneError("AWS credentials unavailable for Neptune signing")
            request = AWSRequest(method=method, url=url, data=body, headers=headers)
            SigV4Auth(credentials.get_frozen_credentials(), "neptune-db", self.region).add_auth(request)
            return dict(request.headers.items())
        except NeptuneError:
            raise
        except Exception:
            raise NeptuneError("Neptune IAM signing failed") from None


class NeptuneClient:
    """Send form-encoded POST /openCypher requests over verified HTTPS/HTTP 1.1.

    Pass an httpx.Client with MockTransport for offline tests. Injected clients
    remain owned by their caller. No automatic retries: a timed-out write may
    already have committed. Redirects are never followed with signed requests.
    """

    def __init__(self, endpoint: str, *, port: int = 8182, timeout: float = 10,
                 iam_auth_enabled: bool = True, region: str | None = None,
                 http_client: httpx.Client | None = None, signer: RequestSigner | None = None):
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", endpoint):
            raise ValueError("Neptune endpoint must be a hostname without scheme, path or credentials")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Neptune port must be between 1 and 65535")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Neptune timeout must be positive and finite")
        if iam_auth_enabled and not region:
            raise ValueError("Neptune region is required with IAM authentication")
        self.url = f"https://{endpoint}:{port}/openCypher"
        self.timeout = timeout
        self._signer = (signer or SigV4Signer(region)) if iam_auth_enabled else None
        self._owns_client = http_client is None
        self._http = http_client if http_client is not None else httpx.Client(
            http2=False, verify=True, trust_env=False,
        )

    @classmethod
    def from_settings(cls, settings, **injected):
        """Explicit opt-in factory; never called by the existing FastAPI wiring."""
        if not settings.neptune_endpoint:
            raise ValueError("Neptune endpoint is not configured")
        return cls(settings.neptune_endpoint, port=settings.neptune_port,
                   timeout=settings.neptune_timeout,
                   iam_auth_enabled=settings.neptune_iam_auth_enabled,
                   region=settings.neptune_region, **injected)

    def execute(self, query: str, parameters: dict) -> list[dict]:
        encoded_parameters = json.dumps(parameters, separators=(",", ":"), allow_nan=False)
        body = urlencode({"query": query, "parameters": encoded_parameters}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        if self._signer is not None:
            headers = self._signer.sign("POST", self.url, body, headers)
        try:
            response = self._http.post(self.url, content=body, headers=headers,
                                       timeout=self.timeout, follow_redirects=False)
        except httpx.TimeoutException:
            raise NeptuneTransportError("Neptune request timed out; write outcome may be unknown") from None
        except httpx.RequestError:
            raise NeptuneTransportError("Neptune network request failed") from None
        if not 200 <= response.status_code < 300:
            raise NeptuneHTTPError(response.status_code)
        try:
            document = response.json()
        except ValueError:
            raise NeptuneResponseError("Neptune returned invalid JSON") from None
        if (not isinstance(document, dict) or set(document) != {"results"}
                or not isinstance(document["results"], list)
                or any(not isinstance(row, dict) for row in document["results"])):
            raise NeptuneResponseError("Neptune returned an invalid results envelope")
        return document["results"]

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
