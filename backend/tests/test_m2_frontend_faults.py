"""Browser fault states against a disposable, in-memory preference endpoint."""
import copy
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_preference_faults_and_refresh():
    playwright = pytest.importorskip("playwright.sync_api")
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    default = {"values": {"answer_style": "normal", "knowledge_level": "intermediate",
                          "output_preferences": []},
               "sources": dict.fromkeys(("answer_style", "knowledge_level", "output_preferences"), "default"),
               "redis_fallback": False}
    state = {"snapshot": copy.deepcopy(default), "mode": "fallback", "puts": 0}
    with tempfile.TemporaryDirectory(prefix="rag-prefs-fault-ui-") as temp:
        dist = Path(temp) / "dist"
        subprocess.run(["node", "node_modules/vite/bin/vite.js", "build", "--outDir", str(dist),
                        "--logLevel", "error"], cwd=ROOT / "frontend", check=True)
        app = FastAPI()

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
                    try:
                        page = browser.new_page()
                        origin = f"http://127.0.0.1:{port}"

                        def route_request(route):
                            request = route.request
                            if request.url == origin + "/api/memory/preferences":
                                if request.method == "GET":
                                    snapshot = copy.deepcopy(state["snapshot"])
                                    snapshot["redis_fallback"] = state["mode"] == "fallback"
                                    route.fulfill(status=200, json=snapshot)
                                elif request.method == "PUT":
                                    state["puts"] += 1
                                    if state["mode"] == "committed_readback_failed":
                                        state["snapshot"]["values"].update(request.post_data_json)
                                        state["snapshot"]["sources"].update(dict.fromkeys(request.post_data_json, "explicit"))
                                    route.fulfill(status=503, json={"message": "simulated storage failure"})
                                else:
                                    raise AssertionError(request.method)
                            elif request.url.startswith(origin + "/"):
                                route.continue_()
                            else:
                                route.abort()

                        page.route("**/*", route_request)
                        page.goto(origin + "/settings", wait_until="networkidle")
                        expect = playwright.expect
                        concise = page.get_by_role("radio", name="简洁", exact=True)
                        expect(page.get_by_text("系统默认", exact=True)).to_have_count(3)
                        expect(page.get_by_text("偏好存储暂时不可用，本次显示系统默认值。", exact=True)).to_be_visible()
                        expect(concise).to_be_disabled()
                        assert state["puts"] == 0

                        state["mode"] = "failed_write"
                        page.get_by_role("button", name="重新读取").click()
                        expect(concise).to_be_enabled()
                        concise.click()
                        expect(page.get_by_text("未能确认偏好已保存，请重新读取。", exact=True)).to_be_visible()
                        expect(concise).to_be_disabled()
                        expect(page.get_by_role("radio", name="默认（正常）", exact=True)).to_be_checked()
                        assert state["snapshot"] == default and state["puts"] == 1
                        page.get_by_role("button", name="重新读取").click()
                        expect(concise).to_be_enabled()
                        expect(page.get_by_text("系统默认", exact=True)).to_have_count(3)

                        state["mode"] = "committed_readback_failed"
                        concise.click()
                        expect(page.get_by_text("未能确认偏好已保存，请重新读取。", exact=True)).to_be_visible()
                        expect(concise).to_be_disabled()
                        expect(page.get_by_role("radio", name="默认（正常）", exact=True)).to_be_checked()
                        assert state["snapshot"]["values"]["answer_style"] == "concise" and state["puts"] == 2
                        state["mode"] = "healthy"
                        page.get_by_role("button", name="重新读取").click()
                        expect(concise).to_be_checked()
                        expect(page.get_by_text("用户自定义", exact=True)).to_have_count(1)
                        expect(page.get_by_text("系统默认", exact=True)).to_have_count(2)
                        page.reload(wait_until="networkidle")
                        expect(concise).to_be_checked()
                        expect(page.get_by_text("用户自定义", exact=True)).to_have_count(1)
                    finally:
                        browser.close()
            finally:
                web.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive()
