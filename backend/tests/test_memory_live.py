"""Actual Redis/HTTP durability and optional browser integration; no shared Redis keys."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis as AsyncRedis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.api.memory import router
from app.services.memory import MemoryService, get_memory_service

ROOT = Path(__file__).resolve().parents[2]
KEY = "rag:memory:preferences:default"
API = "/api/memory/preferences"


@contextmanager
def local_redis(directory, *, aof=True):
    """Own the process, port, password and files. Never open the application's Redis URL."""
    command = os.getenv("RAG_TEST_REDIS_CMD")
    if command:
        binary = json.loads(command)
    elif shutil.which("redis-server"):
        binary = ["redis-server"]
    else:
        pytest.skip("Set RAG_TEST_REDIS_CMD to a local redis-server command array")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    secret = secrets.token_hex(24)
    directory.mkdir(exist_ok=True)
    # A relative config also works when a Windows test process launches a WSL binary.
    settings = (ROOT / "config/redis-preferences.conf").read_text(encoding="utf-8")
    (directory / "test.conf").write_text(settings + f"\nrequirepass {secret}\n", encoding="utf-8")
    url = f"redis://:{secret}@127.0.0.1:{port}/0"
    admin = redis.Redis.from_url(url, decode_responses=True, socket_timeout=1,
                                 socket_connect_timeout=0.3, retry=Retry(NoBackoff(), 0))
    # A WSL relay can keep its Windows launch directory open after the server
    # exits. Give Redis the data directory explicitly and launch from the repo.
    service_dir = str(directory.resolve())
    if Path(binary[0]).stem.casefold() == "wsl":
        service_dir = subprocess.check_output(
            [*binary[:binary.index("--exec") + 1], "wslpath", "-a", "-u", service_dir],
            text=True, cwd=ROOT,
        ).strip()
    cmd = [*binary, f"{service_dir}/test.conf", "--dir", service_dir,
           "--port", str(port), "--appendonly", "yes" if aof else "no"]
    with (directory / "test-server.log").open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        ready = False
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    ready = admin.ping()
                    if ready:
                        break
                except (redis.ConnectionError, redis.TimeoutError):
                    pass
                time.sleep(0.1)
            assert ready, "Isolated Redis did not listen; inspect test-server.log"
            yield url, admin
        finally:
            try:
                if ready and proc.poll() is None:
                    admin.shutdown(nosave=True)
            finally:
                admin.close()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


@contextmanager
def preference_api(url, chat_patch=None):
    client = AsyncRedis.from_url(url, decode_responses=True)
    service = MemoryService(client=client)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_memory_service] = lambda: service
    if chat_patch is not None:
        from app.api.chat import router as chat_router
        from app.core.database import get_session
        from app.services import chat, memory
        app.include_router(chat_router, prefix="/api/v1")
        app.dependency_overrides[get_session] = lambda: None
        chat_patch.setattr(memory, "get_memory_service", lambda: service)
        # An explicit command must complete even without a model or index.
        chat_patch.setattr(chat, "LLMService", lambda: None)
        chat_patch.setattr(chat, "get_retrieval_service", lambda: None)
    with TestClient(app) as http:
        try:
            yield http, service
        finally:
            http.portal.call(client.aclose)


def test_redis_hash_restart_and_independent_backup(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="rag-prefs-durability-") as temp:
        root = Path(temp)
        primary, recovery = root / "primary", root / "recovery"
        backup = root / "backup.rdb"
        with local_redis(primary) as (url, admin), preference_api(url, monkeypatch) as (http, service):
            assert http.get(API).json()["sources"]["answer_style"] == "default"
            assert not admin.exists(KEY)
            turn = http.post("/api/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "以后回答都简洁一点。"}], "kb_ids": [],
            })
            assert turn.status_code == 200 and "event: done" in turn.text and "已保存" in turn.text
            assert http.get(API).json()["values"]["answer_style"] == "concise"
            assert http.put(API, json={"answer_style": "normal"}).json()["sources"]["answer_style"] == "explicit"

            async def add_tags():
                await asyncio.gather(service.change_output_preferences(add=["table"]),
                                     service.change_output_preferences(add=["step_by_step"]))
            http.portal.call(add_tags)
            assert set(json.loads(admin.hget(KEY, "output_preferences"))) == {"table", "step_by_step"}
            assert admin.type(KEY) == "hash" and admin.ttl(KEY) == -1
            assert admin.config_get("appendonly")["appendonly"] == "yes"
            assert admin.config_get("appendfsync")["appendfsync"] == "everysec"
            assert admin.config_get("save")["save"] == "900 1 300 10"
            assert admin.config_get("maxmemory-policy")["maxmemory-policy"] == "noeviction"
            # Snapshot contains the earlier state. Only AOF can replay the later expert change.
            admin.bgsave()
            deadline = time.monotonic() + 10
            while admin.info("persistence")["rdb_bgsave_in_progress"] and time.monotonic() < deadline:
                time.sleep(0.05)
            assert admin.info("persistence")["rdb_last_bgsave_status"] == "ok"
            shutil.copyfile(primary / "dump.rdb", backup)
            checksum = hashlib.sha256(backup.read_bytes()).hexdigest()
            assert http.put(API, json={"knowledge_level": "expert"}).status_code == 200
            first_id = admin.info("server")["run_id"]
            version = admin.info("server")["redis_version"]
        with local_redis(primary) as (url, admin), preference_api(url) as (http, _):
            assert admin.info("server")["run_id"] != first_id
            reloaded = http.get(API).json()
            assert reloaded["sources"] == dict.fromkeys(("answer_style", "knowledge_level", "output_preferences"), "explicit")
            assert reloaded["values"]["knowledge_level"] == "expert" and not reloaded["redis_fallback"]
            assert set(reloaded["values"]["output_preferences"]) == {"table", "step_by_step"}
        recovery.mkdir()
        shutil.copyfile(backup, recovery / "dump.rdb")
        assert hashlib.sha256((recovery / "dump.rdb").read_bytes()).hexdigest() == checksum
        with local_redis(recovery, aof=False) as (url, admin), preference_api(url) as (http, _):
            restored = http.get(API).json()
            assert restored["sources"]["answer_style"] == "explicit"
            assert restored["values"]["answer_style"] == "normal"
            assert restored["sources"]["knowledge_level"] == "default"
            assert set(restored["values"]["output_preferences"]) == {"table", "step_by_step"}
            admin.config_set("appendonly", "yes")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                info = admin.info("persistence")
                if not info["aof_rewrite_in_progress"] and info["aof_enabled"] and info["aof_last_bgrewrite_status"] == "ok":
                    break
                time.sleep(0.05)
            assert admin.info("persistence")["aof_enabled"]
        with local_redis(recovery) as (url, _), preference_api(url) as (http, _):
            assert http.get(API).json() == restored
        print(f"Redis {version}: sparse hash / TTL -1 / concurrent tags / AOF restart / independent RDB restore + AOF restart")


def test_settings_browser_with_real_preference_api():
    """Built settings page -> real HTTP router -> disposable Redis. Other settings use fixtures."""
    playwright = pytest.importorskip("playwright.sync_api")
    import uvicorn
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    with tempfile.TemporaryDirectory(prefix="rag-prefs-browser-") as temp:
        root = Path(temp)
        dist = root / "dist"
        subprocess.run(["node", "node_modules/vite/bin/vite.js", "build", "--outDir", str(dist),
                        "--logLevel", "error"], cwd=ROOT / "frontend", check=True)
        with local_redis(root / "data") as (url, admin):
            from contextlib import asynccontextmanager
            db = AsyncRedis.from_url(url, decode_responses=True)

            @asynccontextmanager
            async def lifespan(_app):
                yield
                await db.aclose()

            app = FastAPI(lifespan=lifespan)
            app.include_router(router)
            service = MemoryService(client=db)
            app.dependency_overrides[get_memory_service] = lambda: service

            @app.get("/api/v1/{path:path}")
            async def other_settings(path: str):
                return {"data": {} if path == "settings" else []}

            @app.get("/settings")
            async def settings_page():
                return FileResponse(dist / "index.html")

            app.mount("/", StaticFiles(directory=dist))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                web = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
                thread = threading.Thread(target=web.run, kwargs={"sockets": [sock]}, daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 10
                    while not web.started and thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.03)
                    assert web.started
                    with playwright.sync_playwright() as pw:
                        browser = pw.chromium.launch(executable_path=os.getenv("RAG_TEST_BROWSER"), headless=True)
                        page = browser.new_page()
                        origin = f"http://127.0.0.1:{port}"
                        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
                        page.goto(origin + "/settings", wait_until="networkidle")
                        expect = playwright.expect
                        expect(page.get_by_text("系统默认", exact=True)).to_have_count(3)
                        page.get_by_role("radio", name="简洁", exact=True).click()
                        expect(page.get_by_text("用户自定义", exact=True)).to_have_count(1)
                        page.get_by_role("radio", name="初学者", exact=True).click()
                        expect(page.get_by_role("radio", name="初学者", exact=True)).to_be_checked()
                        table = page.get_by_role("checkbox", name="表格", exact=True)
                        steps = page.get_by_role("checkbox", name="分步说明", exact=True)
                        table.click()
                        expect(table).to_be_checked()
                        steps.click()
                        expect(steps).to_be_checked()
                        table.click()
                        expect(page.get_by_role("checkbox", name="表格", exact=True)).not_to_be_checked()
                        assert json.loads(admin.hget(KEY, "output_preferences")) == ["step_by_step"]
                        page.reload(wait_until="networkidle")
                        expect(page.get_by_role("radio", name="简洁", exact=True)).to_be_checked()
                        expect(page.get_by_role("radio", name="初学者", exact=True)).to_be_checked()
                        expect(page.get_by_role("checkbox", name="分步说明", exact=True)).to_be_checked()
                        page.get_by_role("button", name="恢复解释深度默认值").click()
                        expect(page.get_by_role("radio", name="默认（中级）", exact=True)).to_be_checked()
                        page.get_by_role("button", name="恢复全部默认值").click()
                        expect(page.get_by_text("系统默认", exact=True)).to_have_count(3)
                        assert not admin.exists(KEY)
                        browser.close()
                finally:
                    web.should_exit = True
                    thread.join(timeout=10)
                    assert not thread.is_alive()
        print("Browser -> FastAPI -> Redis: field origins, two output tags, removal, reload, field/all reset")
