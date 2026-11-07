"""Opt-in Tavily search. Only the current question crosses this boundary."""
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings


@dataclass
class WebSearchResult:
    status: str
    query: str
    results: list[dict[str, Any]] = field(default_factory=list)
    request_id: str | None = None
    usage: dict[str, Any] | None = None
    message: str = ""
    provider_query: str | None = None

    def metadata(self):
        return {"status": self.status, "query": self.query,
                "request_id": self.request_id, "usage": self.usage,
                "provider_query": self.provider_query, "message": self.message}


def _read_api_key(settings):
    key = settings.TAVILY_API_KEY.strip()
    if key:
        return key
    if not settings.TAVILY_API_KEY_FILE:
        return ""
    try:
        path = Path(settings.TAVILY_API_KEY_FILE)
        if path.stat().st_size > 8192:
            return ""
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError, UnicodeError):
        # Never expose a secret, file contents, or a deployment-local path.
        return ""


def _usable_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and not parsed.username and not parsed.password)
    except ValueError:
        return False


async def search_web(query: str) -> WebSearchResult:
    """One bounded request, without retries or private history/KB context."""
    settings = get_settings()
    key = _read_api_key(settings)
    if not key:
        return WebSearchResult("NOT_CONFIGURED", query, message="联网搜索尚未配置 Tavily 密钥。")
    if not query.strip() or len(query) > 1500:
        return WebSearchResult("FAILED", query, message="联网搜索问题须为 1–1500 个字符，请精简后再试。")
    timeout = min(30.0, max(1.0, settings.TAVILY_TIMEOUT_SECONDS))
    limit = min(5, max(1, settings.TAVILY_MAX_RESULTS))
    payload = {"query": query, "search_depth": "basic", "topic": "general",
               "max_results": limit, "auto_parameters": False,
               "include_answer": False, "include_raw_content": False,
               "include_images": False, "include_usage": True}
    request_id = None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await asyncio.wait_for(client.post(
                "https://api.tavily.com/search", json=payload,
                headers={"Authorization": "Bearer " + key}), timeout=timeout)
        request_id = response.headers.get("x-request-id")
        if response.status_code != 200:
            state = "UNKNOWN" if response.status_code >= 500 else "FAILED"
            return WebSearchResult(state, query, request_id=request_id,
                                   message="联网搜索服务未返回可用结果，未自动重试。")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("Invalid search response")
        request_id = data.get("request_id") if isinstance(data.get("request_id"), str) else request_id
        results = []
        for row in data["results"][:limit]:
            if not isinstance(row, dict) or not _usable_url(row.get("url")):
                continue
            content = row.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            title = row.get("title") if isinstance(row.get("title"), str) else row["url"]
            score = row.get("score")
            results.append({"source_type": "web", "url": row["url"], "title": title,
                            "content": content, "score": score if isinstance(score, (int, float)) else 0.0,
                            "fileName": title, "kbName": "互联网", "location": row["url"]})
        return WebSearchResult("SUCCESS" if results else "NO_RESULTS", query, results,
                               request_id, data.get("usage") if isinstance(data.get("usage"), dict) else None,
                               provider_query=data.get("query") if isinstance(data.get("query"), str) else None)
    except (httpx.TimeoutException, httpx.TransportError, asyncio.TimeoutError, ValueError):
        # The provider may have accepted/billed this request. No replay or success claim.
        return WebSearchResult("UNKNOWN", query, request_id=request_id,
                               message="联网搜索结果未确认（超时、连接中断或响应异常），未自动重试。")
