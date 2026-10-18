"""A tenant's endpoint + key is probed before it is saved.

The user complaint this answers: "user cannot add the endpoint" — a form that
saves a key nobody ever tried is how a broken config reaches production.  The
probe answers "does this key work against this endpoint?", and because the URL
is tenant-supplied it also has to be a request the tenant cannot aim at the
network the core runs on.
"""

import httpx
import pytest

from core.application.use_cases import AIConfigUseCases
from core.domain.entities import AIConfig
from core.domain.value_objects import AIProviderType
from core.infrastructure.ai.providers import (
    ProbeResult,
    endpoint_guard,
    probe_credentials,
)


def _transport(handler, calls: list):
    def _record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.MockTransport(_record)


def _json(status: int, body: str = ""):
    return lambda request: httpx.Response(status, text=body, request=request)


def _public_dns(host: str) -> list:
    """The tests must not need a resolver: this is what a public A record is."""
    return ["93.184.216.34"]


async def _probe(provider, model, base_url, api_key, **kw):
    kw.setdefault("resolver", _public_dns)
    return await probe_credentials(provider, model, base_url, api_key, **kw)


# --------------------------------------------------------------------------- #
# the happy path and the refusals a provider actually sends
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_working_key_reports_healthy_and_a_latency():
    calls: list = []
    res = await _probe(
        "openai", "gpt-4o-mini", "https://api.openai.com/v1", "sk-good",
        transport=_transport(_json(200, '{"choices":[]}'), calls),
    )
    assert isinstance(res, ProbeResult)
    assert (res.ran, res.healthy, res.error_code) == (True, True, "")
    assert res.latency_ms >= 0
    assert res.detail == ""
    assert len(calls) == 1
    # The probe asks the provider the same way a rewrite would.
    assert calls[0].url.path.endswith("/chat/completions")
    assert calls[0].headers["authorization"] == "Bearer sk-good"


@pytest.mark.asyncio
async def test_a_rejected_key_is_reported_as_invalid_key_without_echoing_it():
    key = "sk-live-SUPER-SECRET-9d2f"
    body = '{"error":{"message":"Incorrect API key provided: ' + key + '"}}'
    res = await _probe(
        "openai", "gpt-4o-mini", "https://api.openai.com/v1", key,
        transport=_transport(_json(401, body), []),
    )
    assert (res.ran, res.healthy, res.error_code) == (True, False, "invalid_key")
    # The provider quotes the key back at us; the tenant must never see it in a
    # response body that lands in a UI, a log, or a chat.
    assert key not in res.detail
    assert "[REDACTED]" in res.detail


@pytest.mark.asyncio
async def test_forbidden_is_also_an_invalid_key():
    res = await _probe(
        "groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1", "k",
        transport=_transport(_json(403, "forbidden"), []),
    )
    assert res.error_code == "invalid_key"


@pytest.mark.asyncio
async def test_a_wrong_path_is_endpoint_not_found_and_keeps_the_provider_message():
    res = await _probe(
        "custom", "m", "https://gw.example.com/v1", "k",
        transport=_transport(_json(404, '{"error":"no such route"}'), []),
    )
    assert (res.healthy, res.error_code) == (False, "endpoint_not_found")
    assert "no such route" in res.detail


@pytest.mark.asyncio
async def test_a_missing_model_is_reported_with_the_providers_own_words():
    res = await _probe(
        "openai", "not-a-model", "https://api.openai.com/v1", "k",
        transport=_transport(_json(400, '{"error":{"message":"model not-a-model does not exist"}}'), []),
    )
    assert (res.healthy, res.error_code) == (False, "probe_rejected")
    assert "does not exist" in res.detail


@pytest.mark.asyncio
async def test_a_throttled_key_is_rate_limited_not_invalid():
    res = await _probe(
        "openai", "gpt-4o-mini", "https://api.openai.com/v1", "k",
        transport=_transport(_json(429, "slow down"), []),
    )
    assert (res.healthy, res.error_code) == (False, "rate_limited")


@pytest.mark.asyncio
async def test_a_server_error_does_not_leak_the_providers_internals():
    res = await _probe(
        "openai", "gpt-4o-mini", "https://api.openai.com/v1", "k",
        transport=_transport(_json(500, "Traceback: /srv/app/secret_config.py line 42"), []),
    )
    assert (res.healthy, res.error_code) == (False, "provider_error")
    assert res.detail == "HTTP 500"
    assert "secret_config" not in res.detail


@pytest.mark.asyncio
async def test_a_timeout_says_so_and_still_counts_as_ran():
    def _slow(request):
        raise httpx.TimeoutException("timed out", request=request)

    res = await _probe(
        "openai", "gpt-4o-mini", "https://api.openai.com/v1", "k",
        timeout=0.01, transport=_transport(_slow, []),
    )
    assert (res.ran, res.healthy, res.error_code) == (True, False, "timeout")
    assert "0s" in res.detail or "no answer" in res.detail


@pytest.mark.asyncio
async def test_a_connection_failure_names_no_internal_host():
    def _refused(request):
        raise httpx.ConnectError("Connection refused to 172.18.0.4:50051", request=request)

    res = await _probe(
        "openai", "gpt-4o-mini", "https://api.openai.com/v1", "k",
        transport=_transport(_refused, []),
    )
    assert (res.ran, res.healthy, res.error_code) == (True, False, "endpoint_unreachable")
    assert res.detail == "ConnectError"
    assert "172.18" not in res.detail


@pytest.mark.asyncio
async def test_a_parameter_rename_is_not_reported_as_a_bad_key():
    """Newer OpenAI models reject `max_tokens`; a good key must still pass."""
    seen: list = []

    def _handler(request):
        body = request.content.decode()
        seen.append(body)
        if "max_tokens" in body and "max_completion_tokens" not in body:
            return httpx.Response(400, text='{"error":{"message":"Unsupported parameter: max_tokens"}}',
                                  request=request)
        return httpx.Response(200, text='{"choices":[]}', request=request)

    res = await _probe(
        "openai", "gpt-5-mini", "https://api.openai.com/v1", "k",
        transport=_transport(_handler, []),
    )
    assert (res.healthy, res.error_code) == (True, "")
    assert len(seen) == 2 and "max_completion_tokens" in seen[1]


# --------------------------------------------------------------------------- #
# the endpoint guard: a tenant-supplied URL is a server-side request
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("url,code", [
    ("http://127.0.0.1:8080/v1", "endpoint_blocked"),
    ("http://localhost:50051", "endpoint_blocked"),
    ("http://169.254.169.254/latest/meta-data", "endpoint_blocked"),
    ("http://[::1]:9000/v1", "endpoint_blocked"),
    ("http://0.0.0.0:4455/v1", "endpoint_blocked"),
    ("file:///etc/passwd", "endpoint_invalid"),
    ("ftp://gw.example.com", "endpoint_invalid"),
])
async def test_data_resources_are_refused_without_touching_the_network(url, code):
    calls: list = []
    res = await _probe(
        "custom", "m", url, "k", transport=_transport(_json(200), calls),
    )
    assert (res.ran, res.error_code) == (False, code)
    assert calls == [], "a refused endpoint must not be requested"


def test_the_guard_allows_a_public_endpoint_and_an_operator_opt_in():
    assert endpoint_guard("https://api.openai.com/v1") == ""
    assert endpoint_guard("https://gw.example.com:8443/v1", resolver=_public_dns) == ""
    # A self-hosted deployment can opt in; a shared one cannot be talked into it.
    assert endpoint_guard("http://192.168.1.9:1234/v1") == "endpoint_blocked"
    assert endpoint_guard("http://192.168.1.9:1234/v1", allow_private=True) == ""
    assert endpoint_guard("http://localhost:1234/v1", allow_private=True) == "endpoint_blocked"


def test_the_opt_in_reads_the_environment(monkeypatch):
    monkeypatch.setenv("ATF_ALLOW_PRIVATE_AI_ENDPOINTS", "1")
    assert endpoint_guard("http://10.0.0.7:4455/v1") == ""
    monkeypatch.setenv("ATF_ALLOW_PRIVATE_AI_ENDPOINTS", "0")
    assert endpoint_guard("http://10.0.0.7:4455/v1") == "endpoint_blocked"


def test_a_public_name_that_resolves_inside_the_host_is_blocked():
    """`localhost.evil.example` is the oldest trick there is."""
    assert endpoint_guard("https://localhost.evil.example/v1",
                          resolver=lambda host: ["127.0.0.1"]) == "endpoint_blocked"
    assert endpoint_guard("https://gw.example.com/v1",
                          resolver=lambda host: ["93.184.216.34", "10.1.2.3"]) == "endpoint_blocked"


@pytest.mark.asyncio
async def test_a_name_that_does_not_resolve_is_unreachable_not_a_crash():
    calls: list = []

    def _broken(host):
        raise OSError("nodename nor servname provided")

    res = await _probe("custom", "m", "https://nope.example.com/v1", "k",
                       resolver=_broken, transport=_transport(_json(200), calls))
    assert (res.ran, res.error_code) == (False, "endpoint_unreachable")
    assert calls == []


# --------------------------------------------------------------------------- #
# the pre-flight refusals that never reach the network
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,base,key,code", [
    ("", "m", "https://api.openai.com/v1", "k", "missing_provider"),
    ("nope", "m", "https://api.openai.com/v1", "k", "unknown_provider"),
    ("openai", "m", "https://api.openai.com/v1", "", "missing_key"),
    ("custom", "m", "", "k", "missing_endpoint"),
])
async def test_an_incomplete_form_never_leaves_the_process(provider, model, base, key, code):
    calls: list = []
    res = await _probe(provider, model, base, key,
                                  transport=_transport(_json(200), calls))
    assert (res.ran, res.healthy, res.error_code) == (False, False, code)
    assert calls == []


# --------------------------------------------------------------------------- #
# per-provider targets
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_anthropic_uses_its_own_header_and_path():
    calls: list = []
    await _probe("anthropic", "claude-sonnet-4-5", "https://api.anthropic.com/v1", "sk-ant",
                            transport=_transport(_json(200), calls))
    assert calls[0].url.path.endswith("/messages")
    assert calls[0].headers["x-api-key"] == "sk-ant"
    assert "authorization" not in calls[0].headers


@pytest.mark.asyncio
async def test_gemini_uses_the_model_path_and_never_hits_a_guard_refusal():
    calls: list = []
    await _probe("gemini", "gemini-2.5-flash", "", "gk",
                            transport=_transport(_json(200), calls))
    assert f"/models/gemini-2.5-flash:generateContent" in str(calls[0].url)


# --------------------------------------------------------------------------- #
# ownership: the probe spends the tenant's own credentials
# --------------------------------------------------------------------------- #

class _Repo:
    def __init__(self, rows):
        self.rows = {r.id: r for r in rows}
        self.added: list = []

    async def get_by_id(self, config_id):
        return self.rows.get(config_id)

    async def list_by_owner(self, owner):
        return [r for r in self.rows.values() if int(getattr(r, "owner_user_id", 0)) == int(owner)]

    async def add(self, cfg):
        self.added.append(cfg)
        return cfg


def _use_cases(rows):
    from core.infrastructure.ai.providers import AIProviderFactory
    return AIConfigUseCases(_Repo(rows), AIProviderFactory())


@pytest.mark.asyncio
async def test_a_foreign_config_id_cannot_be_probed(monkeypatch):
    seen: list = []

    async def _spy(provider, model, base_url, api_key, **kw):
        seen.append((provider, model, base_url, api_key))
        return ProbeResult(True, True, 1.0, "", "")

    monkeypatch.setattr("core.infrastructure.ai.providers.probe_credentials", _spy)
    uc = _use_cases([AIConfig(id="theirs", provider=AIProviderType.OPENAI, model="gpt-4o-mini",
                              api_key="their-key", owner_user_id=222, base_url="https://api.openai.com/v1")])
    res = await uc.check_provider_key_owned(config_id="theirs", owner_user_id=111)
    assert res.error_code == "config_not_found"
    assert seen == [], "another tenant's key must not be spent"
    # Unknown id and foreign id answer the same way.
    assert (await uc.check_provider_key_owned(config_id="ghost", owner_user_id=111)).error_code == "config_not_found"


@pytest.mark.asyncio
async def test_no_owner_is_a_refusal_not_an_admin_probe(monkeypatch):
    seen: list = []

    async def _spy(*a, **kw):
        seen.append(a)
        return ProbeResult(True, True, 1.0, "", "")

    monkeypatch.setattr("core.infrastructure.ai.providers.probe_credentials", _spy)
    uc = _use_cases([])
    res = await uc.check_provider_key_owned(provider="openai", model="m",
                                            base_url="https://api.openai.com/v1",
                                            api_key="k", owner_user_id=None)
    assert res.error_code == "config_not_found"
    assert seen == []


@pytest.mark.asyncio
async def test_a_stored_key_is_used_without_being_returned(monkeypatch):
    seen: dict = {}

    async def _spy(provider, model, base_url, api_key, **kw):
        seen.update(provider=provider, model=model, base_url=base_url, api_key=api_key)
        return ProbeResult(True, True, 3.0, "", "")

    monkeypatch.setattr("core.infrastructure.ai.providers.probe_credentials", _spy)
    uc = _use_cases([AIConfig(id="mine", provider=AIProviderType.OPENROUTER, model="openai/gpt-5-mini",
                              api_key="stored-secret", owner_user_id=111,
                              base_url="https://openrouter.ai/api/v1")])
    # The UI sends back the mask it was given; that is not a key.
    res = await uc.check_provider_key_owned(config_id="mine", api_key="***cret",
                                            base_url="", model="", owner_user_id=111)
    assert res.healthy
    assert seen == {"provider": "openrouter", "model": "openai/gpt-5-mini",
                    "base_url": "https://openrouter.ai/api/v1", "api_key": "stored-secret"}
    # A dry run never writes.
    assert uc._repo.added == []


@pytest.mark.asyncio
async def test_a_new_key_typed_over_the_stored_one_wins(monkeypatch):
    seen: dict = {}

    async def _spy(provider, model, base_url, api_key, **kw):
        seen.update(api_key=api_key, base_url=base_url)
        return ProbeResult(True, True, 2.0, "", "")

    monkeypatch.setattr("core.infrastructure.ai.providers.probe_credentials", _spy)
    uc = _use_cases([AIConfig(id="mine", provider=AIProviderType.OPENAI, model="gpt-4o-mini",
                              api_key="old", owner_user_id=111, base_url="https://api.openai.com/v1")])
    await uc.check_provider_key_owned(config_id="mine", api_key="brand-new",
                                      base_url="https://gw.example.com/v1", owner_user_id=111)
    assert seen == {"api_key": "brand-new", "base_url": "https://gw.example.com/v1"}
