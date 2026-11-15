"""Single request transport process. Secrets never enter arguments or stdout.

No SDK retries, redirects, tools, proxy environment or arbitrary endpoints.
Parent terminates this process at the deadline; remote usage may remain UNKNOWN.
"""
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
from .locking import atomic_write


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def emit_receipt(value, destination=None):
    # ASCII wire data is locale independent on Windows. Persist the response
    # before using a pipe, so a parent decoder/process failure cannot lose usage.
    body=json.dumps(value,ensure_ascii=True,allow_nan=False).encode('ascii')
    if destination:atomic_write(Path(destination),body,exclusive=True)
    sys.stdout.buffer.write(body+b'\n');sys.stdout.buffer.flush()


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
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    call = urllib.request.Request('https://api.openai.com/v1/responses',
        data=json.dumps(request['body'], ensure_ascii=False, allow_nan=False).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    try:
        with opener.open(call, timeout=request['timeout']) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('response too large')
            emit_receipt({'transport_status': 'RECEIVED', 'http_status': response.status,
                              'provider_request_id': response.headers.get('x-request-id'),
                              'response': json.loads(raw)},request.get('response_path'))
    except urllib.error.HTTPError as error:
        # Never echo provider errors or headers containing request data.
        emit_receipt({'transport_status': 'HTTP_ERROR', 'http_status': error.code,
                          'provider_request_id': error.headers.get('x-request-id')},request.get('response_path'))
    except Exception:
        emit_receipt({'transport_status': 'UNKNOWN'},request.get('response_path'))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        emit_receipt({'transport_status': 'LOCAL_FAILURE'})
