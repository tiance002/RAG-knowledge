"""Check the built Chat page with fake local HTTP/SSE and an ephemeral browser profile."""
import argparse
import functools
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading

from playwright.sync_api import sync_playwright


class SPA(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/chat":
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, *_args):
        pass


def smoke(executable):
    frontend = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="rag-sse-ui-") as output:
        subprocess.run(["node", "node_modules/vite/bin/vite.js", "build", "--outDir", output,
                        "--logLevel", "error"], cwd=frontend, check=True)
        with ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SPA, directory=output)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path=executable, headless=True)
                    for terminal, label in [
                        ("event: error\ndata: {\"status\":\"FAILED\",\"error\":\"服务故障\"}\n\n", "服务故障"),
                        ("event: done\ndata: {\"validation_status\":\"NOT_RUN\"}\n\n", "回答及引用未经逐项核验"),
                        ("event: done\ndata: {\"generation_status\":\"TRUNCATED\"}\n\n", "模型输出达到上限"),
                        ("", "连接中断"),
                    ]:
                        page = browser.new_page()
                        logs = []
                        calls = []
                        page.on("console", lambda msg: logs.append(msg.text))

                        def route(req):
                            url = req.request.url
                            if not url.startswith(origin + "/"):
                                return req.abort()
                            if "/chat/completions" in url:
                                calls.append(req.request.method)
                                body = 'event: answer_chunk\ndata: {"content":"private-marker-answer"}\n\n' + terminal
                                return req.fulfill(content_type="text/event-stream", body=body)
                            if "/api/" in url:
                                return req.fulfill(content_type="application/json", body=json.dumps({"data": []}))
                            return req.continue_()

                        page.route("**/*", route)
                        page.goto(origin + "/chat", wait_until="networkidle")
                        textarea = page.locator("textarea[placeholder]").first
                        textarea.fill("private-marker-question")
                        textarea.press("Enter")
                        page.get_by_text(label, exact=False).first.wait_for(timeout=10000)
                        assert page.get_by_text("private-marker-answer", exact=True).is_visible()
                        assert calls == ["POST"]
                        assert all("private-marker" not in message for message in logs)
                        page.close()
                        print("PASS", label)
                    browser.close()
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--browser", default=None, help="Existing Chromium/Chrome executable")
    smoke(cli.parse_args().browser)
