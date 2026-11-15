"""One SiliconFlow request. Also runnable over SSH, keeping the existing key on ECS."""
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BASE = "https://api.siliconflow.cn/v1"
MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def project_key():
    db = sqlite3.connect("file:/opt/amrag/backend/local.db?mode=ro", uri=True)
    try:
        keys = {key for url, key in db.execute(
            "select base_url,api_key from custom_models where model_name=?", ("zai-org/GLM-5.3-Flash",))
            if url and url.rstrip("/") == BASE and key}
    finally:
        db.close()
    if len(keys) != 1:
        raise RuntimeError("No unique project credential for this endpoint")
    return keys.pop()


def send(body, key):
    request = json.loads(body)
    if (request["model"] != MODEL or request["max_tokens"] != 1200 or request["stream"] is not False
            or request["enable_thinking"] is not False or len(body) > 6000):
        raise ValueError("Request is outside the proposal budget")
    http = build_opener(ProxyHandler({}), NoRedirect())
    req = Request(BASE + "/chat/completions", data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        response = http.open(req, timeout=120)
    except HTTPError as exc:
        response = exc
    with response:
        return {"http_status": response.code, "trace_id": response.headers.get("x-siliconcloud-trace-id"),
                "body": response.read(2_000_000).decode("utf-8")}


if __name__ == "__main__":
    result = send(sys.stdin.buffer.read(6001), project_key())
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode())
