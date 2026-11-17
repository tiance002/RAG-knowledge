"""Synthetic routing checks only; no network or paid generation."""
import urllib.request

import pytest

from rsik.http_worker import build_opener


def capture(monkeypatch, environ):
    seen = {}
    def opener(*handlers):
        seen['handlers'] = handlers
        return seen
    monkeypatch.setattr(urllib.request, 'build_opener', opener)
    return build_opener(environ)['handlers']


def test_ambient_proxy_is_not_trusted(monkeypatch):
    handlers = capture(monkeypatch, {'HTTPS_PROXY': 'http://example.com:1234'})
    assert handlers[0].proxies == {}


@pytest.mark.parametrize('proxy', ['http://127.0.0.1:1234', 'http://localhost:1234', 'http://[::1]:1234'])
def test_explicit_local_proxy(monkeypatch, proxy):
    handlers = capture(monkeypatch, {'RSI_OPENAI_HTTPS_PROXY': proxy})
    assert handlers[0].proxies == {'https': proxy}
    assert handlers[1].redirect_request(None, None, 302, '', {}, 'https://elsewhere') is None


@pytest.mark.parametrize('proxy', ['http://remote.example:1234', 'http://127.0.0.1',
    'https://127.0.0.1:1234', 'http://user:password@127.0.0.1:1234',
    'http://127.0.0.1:1234/path', 'http://127.0.0.1:1234?query=secret',
    'http://127.0.0.1:1234#fragment', 'http://127.0.0.1:bad'])
def test_invalid_proxy_fails_closed(proxy):
    with pytest.raises(ValueError):
        build_opener({'RSI_OPENAI_HTTPS_PROXY': proxy})


def test_explicit_route_ignores_ambient_bypass(monkeypatch):
    handlers = capture(monkeypatch, {'RSI_OPENAI_HTTPS_PROXY': 'http://127.0.0.1:1234'})
    monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda host: pytest.fail('ambient bypass consulted'))
    request = urllib.request.Request('https://api.openai.com/v1/models')
    handlers[0].proxy_open(request, 'http://127.0.0.1:1234', 'https')
    assert request.host == '127.0.0.1:1234'
    assert request._tunnel_host == 'api.openai.com'
    assert request.full_url == 'https://api.openai.com/v1/models'


def test_transport_identity_contains_only_hashes():
    from rsik.http_worker import transport_identity
    direct = transport_identity({})
    assert direct['mode'] == 'DIRECT' and direct['proxy_sha256'] is None
    identity = transport_identity({'RSI_OPENAI_HTTPS_PROXY': 'http://127.0.0.1:1234'})
    assert identity['mode'] == 'LOOPBACK_PROXY'
    assert len(identity['proxy_sha256']) == len(identity['helper_sha256']) == 64
    assert '127.0.0.1' not in str(identity)
    assert direct['helper_sha256'] == identity['helper_sha256']
