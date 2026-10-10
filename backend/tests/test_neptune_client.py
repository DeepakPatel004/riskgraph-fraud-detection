import json
import sys
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import Settings
from app.graph.neptune_client import (
    NeptuneClient, NeptuneError, NeptuneHTTPError, NeptuneResponseError,
    NeptuneTransportError, SigV4Signer,
)


def test_parameterized_form_request_and_timeout():
    query = "MATCH (n {token: $token}) RETURN n"
    params = {"token": "token&+= % ' \u20b9", "optional": None, "ids": ["a", "b"], "count": 2}
    def handle(request):
        assert request.method == "POST"
        assert str(request.url) == "https://neptune.invalid:9191/openCypher"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.headers["accept"] == "application/json"
        assert "authorization" not in request.headers
        assert request.extensions["timeout"]["read"] == 3
        form = parse_qs(request.content.decode())
        assert form["query"] == [query]
        assert json.loads(form["parameters"][0]) == params
        return httpx.Response(200, json={"results": [{"value": 2}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with NeptuneClient("neptune.invalid", port=9191, timeout=3,
                           iam_auth_enabled=False, http_client=http) as client:
            assert client.execute(query, params) == [{"value": 2}]
        assert not http.is_closed  # Injected resources remain caller-owned.


def test_injected_signer_signs_exact_transmitted_bytes_each_time():
    calls = []
    class Signer:
        def sign(self, method, url, body, headers):
            calls.append((method, url, body))
            return headers | {"Authorization": "offline-test-signature"}
    def handle(request):
        assert request.headers["authorization"] == "offline-test-signature"
        assert calls[-1] == (request.method, str(request.url), request.content)
        return httpx.Response(200, json={"results": []})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = NeptuneClient("neptune.invalid", region="ap-south-1", signer=Signer(), http_client=http)
        client.execute("RETURN $x", {"x": "a+b&c"})
        client.execute("RETURN $x", {"x": "other"})
    assert len(calls) == 2


@pytest.mark.parametrize("status", [301, 400, 403, 429, 500])
def test_http_errors_are_safe_and_not_retried(status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="sensitive-token", headers={"Location": "https://elsewhere.invalid"})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = NeptuneClient("neptune.invalid", iam_auth_enabled=False, http_client=http)
        with pytest.raises(NeptuneHTTPError) as error:
            client.execute("RETURN $token", {"token": "sensitive-token"})
        assert error.value.status_code == status
        assert "sensitive-token" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_failures_are_safe(failure):
    def handle(request):
        raise failure("sensitive-token", request=request)
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = NeptuneClient("neptune.invalid", iam_auth_enabled=False, http_client=http)
        with pytest.raises(NeptuneTransportError) as error:
            client.execute("RETURN 1", {})
        assert "sensitive-token" not in str(error.value)


@pytest.mark.parametrize("content", [
    b"not-json", b'{"results": []} trailing-error', b"[]", b"{}",
    b'{"results": {}}', b'{"results": [1]}',
    b'{"results": [], "code": "failure"}',
])
def test_malformed_responses(content):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))) as http:
        client = NeptuneClient("neptune.invalid", iam_auth_enabled=False, http_client=http)
        with pytest.raises(NeptuneResponseError):
            client.execute("RETURN 1", {})


@pytest.mark.parametrize("kwargs", [
    {"endpoint": "http://neptune.invalid"}, {"endpoint": "user:pass@neptune.invalid"},
    {"endpoint": "neptune.invalid/path"}, {"endpoint": "neptune.invalid", "port": 0},
    {"endpoint": "neptune.invalid", "timeout": 0},
    {"endpoint": "neptune.invalid", "timeout": float("inf")},
])
def test_invalid_transport_configuration(kwargs):
    with pytest.raises(ValueError):
        NeptuneClient(**kwargs, iam_auth_enabled=False)


def test_iam_requires_region_and_signing_is_lazy(monkeypatch):
    with pytest.raises(ValueError, match="region"):
        NeptuneClient("neptune.invalid")
    monkeypatch.setattr(SigV4Signer, "sign", lambda *args: pytest.fail("signing during construction"))
    with NeptuneClient("neptune.invalid", region="ap-south-1"):
        pass


def test_settings_are_inert_and_factory_is_explicit():
    settings = Settings(_env_file=None)
    assert settings.neptune_endpoint is None
    with pytest.raises(ValueError, match="not configured"):
        NeptuneClient.from_settings(settings)
    settings = Settings(_env_file=None, neptune_endpoint="neptune.invalid", neptune_iam_auth_enabled=False)
    with NeptuneClient.from_settings(settings) as client:
        assert client.url == "https://neptune.invalid:8182/openCypher"


def test_optional_signing_dependency_has_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "botocore.auth", None)
    with pytest.raises(NeptuneError, match="requirements-neptune.txt"):
        SigV4Signer("ap-south-1").sign("POST", "https://neptune.invalid", b"", {})


def install_fake_botocore(monkeypatch, credentials):
    """Test signer wiring only, not AWS's cryptographic implementation."""
    captured = []
    class FakeRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.headers = dict(self.headers)
    class FakeAuth:
        def __init__(self, frozen, service, region):
            captured.append((frozen, service, region))
        def add_auth(self, request):
            request.headers["Authorization"] = "offline-signature"
            captured.append((request.method, request.url, request.data))
    modules = {name: ModuleType(name) for name in [
        "botocore", "botocore.auth", "botocore.awsrequest", "botocore.session",
    ]}
    modules["botocore.auth"].SigV4Auth = FakeAuth
    modules["botocore.awsrequest"].AWSRequest = FakeRequest
    modules["botocore.session"].get_session = lambda: SimpleNamespace(get_credentials=lambda: credentials)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return captured


def test_botocore_chain_service_region_and_credentials_refresh(monkeypatch):
    frozen = []
    credentials = SimpleNamespace(get_frozen_credentials=lambda: frozen.append(object()) or frozen[-1])
    captured = install_fake_botocore(monkeypatch, credentials)
    signer = SigV4Signer("ap-south-1")
    for _ in range(2):
        headers = signer.sign("POST", "https://neptune.invalid:8182/openCypher", b"encoded-body", {})
        assert headers["Authorization"] == "offline-signature"
    assert captured[0] == (frozen[0], "neptune-db", "ap-south-1")
    assert captured[2] == (frozen[1], "neptune-db", "ap-south-1")
    assert captured[1][2] == b"encoded-body"


def test_missing_credentials_fail_without_http(monkeypatch):
    install_fake_botocore(monkeypatch, None)
    with pytest.raises(NeptuneError, match="credentials unavailable"):
        SigV4Signer("ap-south-1").sign("POST", "https://neptune.invalid", b"", {})


def test_auth_disabled_never_calls_signer():
    class ForbiddenSigner:
        def sign(self, *args):
            pytest.fail("IAM signing was called despite being disabled")
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": []}),
    )) as http:
        client = NeptuneClient("neptune.invalid", iam_auth_enabled=False,
                               signer=ForbiddenSigner(), http_client=http)
        assert client.execute("RETURN 1", {}) == []
