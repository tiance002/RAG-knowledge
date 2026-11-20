"""Single request transport process. Secrets never enter arguments or stdout.

No SDK retries, redirects, tools, implicit proxy environment or arbitrary endpoints.
Parent terminates this process at the deadline; remote usage may remain UNKNOWN.
"""
import json
import hashlib
import ipaddress
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import urllib.parse
from .locking import atomic_write


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TrustedProxyHandler(urllib.request.ProxyHandler):
    def proxy_open(self, req, proxy, proxy_type):
        # Routing is explicit: ambient NO_PROXY cannot silently bypass the tunnel.
        req.set_proxy(urllib.parse.urlsplit(proxy).netloc, 'http')
        return None


def build_opener(environ=None):
    """Default to direct transport; allow an explicitly trusted local tunnel.

    Generic HTTP(S)_PROXY, platform settings and NO_PROXY never select routing.
    HTTPS still targets api.openai.com and uses normal certificate verification.
    """
    environ = os.environ if environ is None else environ
    proxy = environ.get('RSI_OPENAI_HTTPS_PROXY', '')
    proxies = {}
    if proxy:
        parsed = urllib.parse.urlsplit(proxy)
        try:
            local = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname).is_loopback
        except (ValueError, TypeError):
            local = False
        if (parsed.scheme != 'http' or not local or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or not parsed.port):
            raise ValueError('invalid trusted OpenAI proxy configuration')
        proxies['https'] = proxy
    return urllib.request.build_opener(TrustedProxyHandler(proxies), NoRedirect())


def transport_identity(environ=None):
    """Bind effective routing and this helper without disclosing proxy URLs."""
    environ = os.environ if environ is None else environ
    build_opener(environ)  # Validate before identifying the effective route.
    proxy = environ.get('RSI_OPENAI_HTTPS_PROXY', '')
    return {'mode': 'LOOPBACK_PROXY' if proxy else 'DIRECT',
            'proxy_sha256': hashlib.sha256(proxy.encode('utf-8')).hexdigest() if proxy else None,
            'helper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def emit_receipt(value, destination=None):
    # ASCII wire data is locale independent on Windows. Persist the response
    # before using a pipe, so a parent decoder/process failure cannot lose usage.
    body=json.dumps(value,ensure_ascii=True,allow_nan=False).encode('ascii')
    if destination:atomic_write(Path(destination),body,exclusive=True)
    sys.stdout.buffer.write(body+b'\n');sys.stdout.buffer.flush()


def request_wire_bytes(body):
    return json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')


def main():
    request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    key = None
    for line in Path(request['key_file']).read_text(encoding='utf-8-sig').splitlines():
        name, separator, value = line.strip().removeprefix('export ').partition('=')
        if separator and name.strip() == 'OPENAI_API_KEY':
            key = value.strip().strip('\"\'')
            break
    if not key or not key.startswith('sk-'):
        emit_receipt({'transport_status': 'CREDENTIAL_UNAVAILABLE'},request.get('response_path'))
        return
    identity = transport_identity()
    opener = build_opener()
    call = urllib.request.Request('https://api.openai.com/v1/responses',
        data=request_wire_bytes(request['body']),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    try:
        with opener.open(call, timeout=request['timeout']) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('response too large')
            emit_receipt({'transport_identity': identity, 'transport_status': 'RECEIVED', 'http_status': response.status,
                              'provider_request_id': response.headers.get('x-request-id'),
                              'response': json.loads(raw)},request.get('response_path'))
    except urllib.error.HTTPError as error:
        # Never echo provider errors or headers containing request data.
        emit_receipt({'transport_identity': identity, 'transport_status': 'HTTP_ERROR', 'http_status': error.code,
                          'provider_request_id': error.headers.get('x-request-id')},request.get('response_path'))
    except (TimeoutError, urllib.error.URLError) as error:
        kind = 'TIMEOUT' if isinstance(getattr(error, 'reason', error), TimeoutError) else 'UNKNOWN'
        emit_receipt({'transport_identity': identity, 'transport_status': kind},request.get('response_path'))
    except Exception:
        emit_receipt({'transport_identity': identity, 'transport_status': 'UNKNOWN'},request.get('response_path'))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        emit_receipt({'transport_status': 'LOCAL_FAILURE'})
