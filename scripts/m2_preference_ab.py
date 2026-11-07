"""Isolated real ChatService/BM25/Agent preference experiment.

The execute command makes paid model calls. Preflight and unit tests never do.
All experiment evidence and private source content must remain under project var/.
Credentials are read from M2_AB_API_KEY into memory, never written to SQLite.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "m2-preference-ab/v1"
KB_ID = "m2-ab-kb"
ARMS = {"A": {}, "B": {"answer_style": "detailed"},
        "C": {"knowledge_level": "beginner"},
        "D": {"output_preferences": ["step_by_step"]}}
MODES = ("chat", "agent")
FIXED_SEARCH = {"search_mode": "fulltext", "top_k": 10, "context_window": 0,
                "rerank_enabled": False, "use_rewrite": False, "score_threshold": 0}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def private_path(path):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to((ROOT / "var").resolve()):
        raise ValueError("Experiment inputs and outputs must be under project var")
    return resolved


def endpoint(base_url):
    parsed = urlsplit(base_url)
    # Only the explicitly supported public provider endpoint may leave this process.
    if (parsed.scheme != "https" or parsed.hostname != "api.siliconflow.cn"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1"):
        raise ValueError("Only the approved SiliconFlow HTTPS v1 endpoint is supported")
    return "https://api.siliconflow.cn/v1/chat/completions"


def validate_manifest(manifest):
    if manifest.get("schema_version") != SCHEMA or manifest.get("authorized") is not True:
        raise ValueError("Versioned, explicitly authorized manifest required")
    if not manifest.get("dataset_version") or manifest.get("repetitions") != 2:
        raise ValueError("Dataset version and exactly two repetitions required")
    if manifest.get("model") != "zai-org/GLM-5.3-Flash":
        raise ValueError("Only authorized GLM-5.3-Flash model is supported")
    endpoint(manifest["base_url"])
    budget = Decimal(str(manifest["budget_cny"]))
    if not Decimal("0") < budget <= Decimal("1"):
        raise ValueError("Total experiment budget must be in (0, 1] CNY")
    cap = manifest.get("max_output_tokens")
    if type(cap) is not int or not 128 <= cap <= 4096:
        raise ValueError("Explicit output token cap between 128 and 4096 required")
    pricing = manifest["pricing"]
    if not pricing.get("source_url") or not pricing.get("verified_at"):
        raise ValueError("Pricing attribution and verification time required")
    for key in ("input_cny_per_million", "output_cny_per_million"):
        rate = Decimal(str(pricing[key]))
        if not rate.is_finite() or rate <= 0:
            raise ValueError("Nonzero finite verified prices required")
    sources = manifest.get("sources", [])
    source_ids = {source["id"] for source in sources}
    if not sources or len(source_ids) != len(sources):
        raise ValueError("Unique original source identities required")
    for source in sources:
        path = private_path(source["path"])
        if source.get("authorized") is not True or not source.get("title"):
            raise ValueError("Explicit source authorization and title required")
        if digest(path.read_bytes()) != source.get("sha256"):
            raise ValueError("Original source hash mismatch")
    chunks = manifest.get("chunks", [])
    chunk_ids = {chunk["id"] for chunk in chunks}
    if not chunks or len(chunk_ids) != len(chunks):
        raise ValueError("Unique frozen chunks required")
    for chunk in chunks:
        if (chunk.get("source_id") not in source_ids or not chunk.get("content", "").strip()
                or not chunk.get("locator")
                or digest(chunk["content"].encode("utf-8")) != chunk.get("sha256")):
            raise ValueError("Frozen chunk source, locator or hash invalid")
    cases = manifest.get("cases", [])
    if len(cases) != 6 or len({case["qid"] for case in cases}) != 6:
        raise ValueError("Exactly six distinct frozen cases required")
    if not any(case.get("negative") is True for case in cases):
        raise ValueError("At least one negative control required")
    for case in cases:
        expected = case.get("expected_chunk_ids")
        if (not case.get("question", "").strip() or case.get("kb_scope") != [KB_ID]
                or not isinstance(expected, list) or not set(expected) <= chunk_ids
                or not isinstance(case.get("answer_points"), list) or not case["answer_points"]
                or any(not isinstance(point, str) or not point.strip() for point in case["answer_points"])
                or not case.get("gold_locator") or type(case.get("negative")) is not bool
                or (case["negative"] and expected) or (not case["negative"] and not expected)):
            raise ValueError("Case scope, evidence qrels, answer points or Gold locator invalid")
    return {"status": "PASS", "model_calls": 0, "cases": 6, "arms": ARMS,
            "modes": list(MODES), "repetitions": 2, "planned_trials": 96,
            "manifest_sha256": digest(canonical(manifest)),
            "runner_sha256": digest(Path(__file__).read_bytes()),
            "budget_cny": str(budget), "entry": "ChatService.chat_stream async events; no HTTP server",
            "retrieval": "real fulltext BM25; embedding, rerank and rewrite disabled",
            "fixed_search": FIXED_SEARCH,
            "configuration_scope": "Isolated experiment config; production service defaults untouched; actual Agent queries can miss Gold qrels",
            "source_locator_verification": "Caller must independently verify chunk and Gold locators against originals; hashes alone do not prove textual fidelity"}


class GuardStop(BaseException):
    """Stop the entire experiment, bypassing application retry/fallback handlers."""


class BudgetTransport:
    """One writer, one in-flight request, durable reservation before transmission.

    The input bound is four tokens per serialized UTF-8 byte plus 4096 tokens
    for server chat/tool framing. This deliberately over-reserves short prompts.
    Output is capped in the actual transmitted payload, including reasoning.
    An unknown or absent usage keeps its full reservation and ends the run.
    """

    def __init__(self, manifest, output, *, transport=None):
        self.manifest, self.output = manifest, Path(output)
        self.url = endpoint(manifest["base_url"])
        self.limit = Decimal(str(manifest["budget_cny"]))
        self.input_rate = Decimal(str(manifest["pricing"]["input_cny_per_million"]))
        self.output_rate = Decimal(str(manifest["pricing"]["output_cny_per_million"]))
        self.committed = Decimal(0)
        self.actual_cost = Decimal(0)
        self.calls = []
        self.context = {}
        self.stopped = False
        self.busy = False
        self.inner_transport = transport
        self.ledger = self.output / "provider-ledger.jsonl"
        if self.ledger.exists():
            raise ValueError("Existing provider ledger cannot be replayed or reset")

    def append(self, value):
        with self.ledger.open("ab") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def cost(self, prompt, completion):
        return (Decimal(prompt) * self.input_rate + Decimal(completion) * self.output_rate) / Decimal(1000000)

    async def send(self, sender, client, request, *args, **kwargs):
        import httpx
        if self.stopped or self.busy:
            raise GuardStop("Transport stopped or concurrent model call rejected")
        if str(request.url) != self.url or request.method != "POST":
            self.stopped = True
            raise GuardStop("Unapproved network request blocked")
        try:
            payload = json.loads(request.content)
        except Exception:
            self.stopped = True
            raise GuardStop("Unparseable model payload blocked")
        if payload.get("model") != self.manifest["model"]:
            self.stopped = True
            raise GuardStop("Unapproved model blocked")
        cap = self.manifest["max_output_tokens"]
        for key in ("max_tokens", "max_completion_tokens"):
            if key in payload and (type(payload[key]) is not int or payload[key] <= 0):
                raise GuardStop("Invalid output cap")
        payload["max_tokens"] = min(payload.get("max_tokens", cap), cap)
        if "max_completion_tokens" in payload:
            payload["max_completion_tokens"] = min(payload["max_completion_tokens"], cap)
        if payload.get("stream"):
            payload["stream_options"] = {**payload.get("stream_options", {}), "include_usage": True}
        body = canonical(payload)
        input_bound = len(body) * 4 + 4096
        reserve = self.cost(input_bound, cap)
        if self.committed + reserve > self.limit:
            self.stopped = True
            raise GuardStop("BUDGET_BLOCKED: next conservative reservation exceeds total limit")
        headers = [(key, value) for key, value in request.headers.multi_items() if key.lower() != "content-length"]
        bounded_request = httpx.Request(request.method, request.url, headers=headers, content=body,
                                       extensions=request.extensions)
        call = {"call_id": len(self.calls) + 1, **self.context, "state": "UNKNOWN_PENDING",
                "request_sha256": digest(body), "requested_model": payload["model"],
                "input_token_bound": input_bound, "output_token_bound": cap,
                "reservation_cny": str(reserve), "started_at": time.time()}
        self.committed += reserve
        self.calls.append(call)
        self.append(call)
        # Private prompts stay under var; request headers and credentials are never saved.
        (self.output / f"request-{call['call_id']:04d}.json").write_bytes(body)
        self.busy = True
        started = time.monotonic()
        try:
            response = await sender(client, bounded_request, *args, **kwargs)
            raw = await response.aread()
            (self.output / f"response-{call['call_id']:04d}.body").write_bytes(raw)
            call["http_status"] = response.status_code
            if response.status_code != 200:
                raise ValueError("Provider HTTP result is not successful")
            if payload.get("stream"):
                objects = [json.loads(line[5:].strip()) for line in raw.decode("utf-8").splitlines()
                           if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")]
                usage_rows = [obj for obj in objects if obj.get("usage")]
                data = usage_rows[-1] if usage_rows else {}
                actual_models = {obj.get("model") for obj in objects if obj.get("model")}
                actual_model = next(iter(actual_models)) if len(actual_models) == 1 else None
            else:
                data = json.loads(raw)
                actual_model = data.get("model")
            usage = data.get("usage") or {}
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if (type(prompt) is not int or type(completion) is not int
                    or prompt < 0 or completion < 0 or prompt > input_bound or completion > cap):
                raise ValueError("Missing, invalid or out-of-reservation provider usage")
            if actual_model != self.manifest["model"]:
                raise ValueError("Provider returned unknown or mismatched model identity")
            billed = self.cost(prompt, completion)
            self.committed += billed - reserve
            self.actual_cost += billed
            call.update(state="COMPLETED", actual_model=actual_model, provider_request_id=data.get("id"),
                        usage=usage, price_based_cost_cny=str(billed),
                        cost_status="PRICE_BASED_NOT_PROVIDER_BILL", ended_at=time.time())
            return response
        except BaseException as exc:
            self.stopped = True
            call.update(state="UNKNOWN", error_type=type(exc).__name__, ended_at=time.time())
            raise GuardStop("UNKNOWN provider attempt retained at full reservation; no retry permitted") from None
        finally:
            self.busy = False
            call["latency_ms"] = (time.monotonic() - started) * 1000
            self.append(call)

    @contextmanager
    def installed(self):
        import httpx
        original_async, original_sync = httpx.AsyncClient.send, httpx.Client.send
        guard = self
        async def guarded(client, request, *args, **kwargs):
            if getattr(client, '_m2_authorized_guard', None) is not guard:
                guard.stopped = True
                raise GuardStop("Unbound asynchronous HTTP client blocked")
            return await original_async(client, request, *args, **kwargs)
        def denied(*args, **kwargs):
            guard.stopped = True
            raise GuardStop("Synchronous outbound network blocked")
        httpx.AsyncClient.send, httpx.Client.send = guarded, denied
        try:
            yield
        finally:
            httpx.AsyncClient.send, httpx.Client.send = original_async, original_sync

    @asynccontextmanager
    async def bind_services(self):
        """Explicitly bind BOTH SDKs to the same accounting transport.

        LangChain may select an aiohttp-backed client that overrides HTTPX.send.
        Explicit clients avoid that bypass; accounting lives at the transport.
        """
        import httpx
        from openai import AsyncOpenAI
        from langchain_openai import ChatOpenAI
        from app.services.llm import LLMService
        guard = self
        inner = self.inner_transport or httpx.AsyncHTTPTransport(retries=0)
        class AccountingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                async def dispatch(_client, bounded_request, *args, **kwargs):
                    return await inner.handle_async_request(bounded_request)
                return await guard.send(dispatch, None, request)
            async def aclose(self):
                await inner.aclose()
        async_client = httpx.AsyncClient(transport=AccountingTransport(), trust_env=False, follow_redirects=False)
        async_client._m2_authorized_guard = self
        sync_client = httpx.Client(trust_env=False)
        original_client, original_model = LLMService._get_client, LLMService.get_chat_model
        def get_client(service):
            if service._client is None:
                service._client = AsyncOpenAI(base_url=service.base_url, api_key=service.api_key,
                                             timeout=120, max_retries=0, http_client=async_client)
            return service._client
        def get_model(service, temperature=.5, streaming=True, max_tokens=None):
            return ChatOpenAI(model=service.model, openai_api_key=service.api_key,
                              openai_api_base=service.base_url, temperature=temperature,
                              streaming=streaming, max_tokens=max_tokens, max_retries=0,
                              http_async_client=async_client, http_client=sync_client)
        LLMService._get_client, LLMService.get_chat_model = get_client, get_model
        try:
            yield
        finally:
            LLMService._get_client, LLMService.get_chat_model = original_client, original_model
            await async_client.aclose()
            sync_client.close()

    def summary(self):
        return {"actual_http_calls": len(self.calls), "completed_calls": sum(c["state"] == "COMPLETED" for c in self.calls),
                "unknown_calls": sum(c["state"] != "COMPLETED" for c in self.calls),
                "committed_including_unknown_cny": str(self.committed),
                "price_based_known_cost_cny": str(self.actual_cost), "budget_cny": str(self.limit),
                "provider_bill": "UNKNOWN", "stopped": self.stopped,
                "prompt_tokens": sum(c.get("usage", {}).get("prompt_tokens", 0) for c in self.calls),
                "completion_tokens": sum(c.get("usage", {}).get("completion_tokens", 0) for c in self.calls)}


@contextmanager
def isolated_redis(output):
    """Launch and stop only the new loopback Redis owned by this run."""
    import redis
    command = os.getenv("RAG_TEST_REDIS_CMD")
    binary = json.loads(command) if command else [shutil.which("redis-server") or "redis-server"]
    directory = Path(output) / "redis-runtime"
    directory.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    password = secrets.token_hex(24)
    config = "bind 127.0.0.1\nprotected-mode yes\nappendonly no\nsave \"\"\nmaxmemory-policy noeviction\n"
    (directory / "redis.conf").write_text(config + f"requirepass {password}\n", encoding="utf-8")
    service_dir = str(directory.resolve())
    if Path(binary[0]).stem.casefold() == "wsl":
        service_dir = subprocess.check_output([*binary[:binary.index("--exec") + 1], "wslpath", "-a", "-u", service_dir],
                                              text=True, cwd=ROOT).strip()
    url = f"redis://:{password}@127.0.0.1:{port}/0"
    admin = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=.2)
    process = None
    with (directory / "redis.log").open("ab") as log:
        try:
            process = subprocess.Popen([*binary, f"{service_dir}/redis.conf", "--dir", service_dir, "--port", str(port)],
                                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    if admin.ping():
                        break
                except redis.RedisError:
                    time.sleep(.1)
            else:
                raise RuntimeError("Owned local Redis did not start")
            yield url
        finally:
            if process is not None and process.poll() is None:
                try:
                    admin.shutdown(nosave=True)
                except redis.RedisError:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            admin.close()
            # Password is no longer needed after the owned process exits.
            (directory / "redis.conf").unlink(missing_ok=True)


def configure_runtime(manifest, output, redis_url):
    secret = os.getenv("M2_AB_API_KEY", "")
    if not secret:
        raise ValueError("M2_AB_API_KEY missing; no model calls made")
    os.environ.update(ENV="test", DATABASE_URL="sqlite:///" + str(output / "isolated.db"),
                      CHROMA_DB_DIR=str(output / "chroma"), UPLOAD_DIR=str(output / "uploads"),
                      IMAGE_DIR=str(output / "images"), MEMORY_REDIS_URL=redis_url, REDIS_URL=redis_url,
                      LLM_BASE_URL=manifest["base_url"], LLM_API_KEY=secret, LLM_MODEL=manifest["model"],
                      LLM_ANSWER_MAX_TOKENS=str(manifest["max_output_tokens"]),
                      EMBEDDING_BASE_URL="http://127.0.0.1:1", EMBEDDING_API_KEY="disabled", EMBEDDING_MODEL="disabled",
                      RAG_MODEL_ROUTER_MODE="OFF", RAG_GROUNDED_GENERATION="true", LANGFUSE_ENABLED="false",
                      RAG_METRICS_ENABLED="false", RAG_OBSERVABILITY_ENABLED="true", RAG_SNAPSHOTS_ENABLED="false",
                      RAG_OBSERVATION_LOG=str(output / "observations.jsonl"), ANONYMIZED_TELEMETRY="False")
    sys.path.insert(0, str(ROOT / "backend"))
    os.chdir(output)


def build_index(manifest):
    from sqlmodel import SQLModel, Session
    from app.core.database import engine
    from app.models import KnowledgeBase, FileDocument, DocumentChunk, FileStatus
    from app.services.bm25 import BM25Index
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id=KB_ID, name="M2 frozen authorized evidence", embedding_model="disabled",
                                  vlm_model="disabled", chunk_count=len(manifest["chunks"])))
        for source in manifest["sources"]:
            session.add(FileDocument(id=source["id"], kb_id=KB_ID, name=source["title"],
                                     local_path=source["path"], size=Path(source["path"]).stat().st_size,
                                     status=FileStatus.READY, progress=100))
        session.commit()
        index = BM25Index()
        for number, chunk in enumerate(manifest["chunks"]):
            session.add(DocumentChunk(id=chunk["id"], file_id=chunk["source_id"], content=chunk["content"],
                                      page_number=chunk.get("page_number"), original_index=number))
            session.flush()
            index.index_chunk(session, chunk["id"], chunk["content"], KB_ID)
        session.commit()


def trial_plan(manifest):
    for case in manifest["cases"]:
        for mode in MODES:
            for repetition in range(1, 3):
                for arm, preferences in ARMS.items():
                    yield case, mode, repetition, arm, preferences

async def run_trials(manifest, output, guard):
    async with guard.bind_services():
        return await _run_trials(manifest, output, guard)


async def _run_trials(manifest, output, guard):
    from app.services.chat import ChatService
    from app.services.memory import get_memory_service, PreferenceScope
    from app.schemas.schemas import Message
    from app.services.trace_schema import capture_scope
    memory = get_memory_service()
    service = ChatService()
    trials = []
    try:
        for case, mode, repetition, arm, preferences in trial_plan(manifest):
            scope = PreferenceScope(tenant_id="m2-ab", user_id=f"r{repetition}-{mode}-{arm}")
            await memory.reset_preferences(scope=scope)
            if preferences:
                await memory.update_preference(preferences, scope=scope)
            identity = {"qid": case["qid"], "mode": mode, "arm": arm, "repetition": repetition,
                        "dataset_version": manifest["dataset_version"], "run_id": output.name,
                        "kb_scope": [KB_ID]}
            guard.context = identity
            before = len(guard.calls)
            started = time.monotonic()
            record = {**identity, "state": "STARTED", "events": [], "entry": "ChatService.chat_stream",
                      "preferences": (await memory.get_preferences(scope=scope)).model_dump()}
            try:
                with capture_scope(run_id=output.name, attempt_id=f"{mode}-{arm}-r{repetition}-{digest(case['qid'].encode())[:12]}",
                                   qid=case['qid'], dataset_version=manifest['dataset_version']):
                    async with asyncio.timeout(480):
                        async for event, data in service.chat_stream(
                            messages=[Message(role="user", content=case["question"])], kb_ids=[KB_ID],
                            mode=mode, model_id=None, memory_scope=scope, **FIXED_SEARCH):
                            record["events"].append({"event": event, "data": data})
                record["state"] = "RETURNED" if any(e["event"] == "done" for e in record["events"]) else "NO_DONE"
                if record["state"] != "RETURNED" or any(e["event"] == "error" for e in record["events"]):
                    raise GuardStop("Application failed or emitted no done event; no automatic retry")
            except GuardStop:
                record["state"] = "GUARD_STOP"
                raise
            except BaseException as exc:
                record.update(state="FAILED", error_type=type(exc).__name__)
                raise GuardStop("Trial failed; stop without automatic retry") from None
            finally:
                record.update(latency_ms=(time.monotonic() - started) * 1000,
                              provider_call_ids=[c["call_id"] for c in guard.calls[before:]])
                trials.append(record)
                with (output / "trials.jsonl").open("ab") as stream:
                    stream.write(canonical(record) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
    finally:
        await memory.client.aclose()
    return trials


def execute(manifest, output):
    preflight = validate_manifest(manifest)
    if not os.getenv("M2_AB_API_KEY"):
        raise ValueError("M2_AB_API_KEY missing; no model calls made")
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_bytes(canonical(manifest))
    (output / "preflight.json").write_bytes(canonical(preflight))
    guard = BudgetTransport(manifest, output)
    summary = {"status": "NOT_RUN", "planned_trials": 96, "entry": preflight["entry"]}
    original_cwd = Path.cwd()
    try:
        with isolated_redis(output) as redis_url:
            configure_runtime(manifest, output, redis_url)
            # All imports and initialization run behind the same outbound deny rule.
            with (output / "runtime.log").open("w", encoding="utf-8") as stream:
                from contextlib import redirect_stdout, redirect_stderr
                with redirect_stdout(stream), redirect_stderr(stream):
                    with guard.installed():
                        build_index(manifest)
                        asyncio.run(run_trials(manifest, output, guard))
            summary["status"] = "RECORDED_REQUIRES_EVIDENCE_REVIEW"
    except GuardStop as exc:
        summary.update(status="STOPPED", stop_reason=str(exc))
    except Exception as exc:
        summary.update(status="FAILED", error_type=type(exc).__name__)
    finally:
        os.chdir(original_cwd)
        summary.update(guard.summary())
        trials = output / "trials.jsonl"
        summary["recorded_trials"] = sum(bool(line.strip()) for line in trials.read_bytes().splitlines()) if trials.exists() else 0
        summary["correctness"] = "NOT_SCORED; inspect source/Gold and answers independently"
        summary["resources"] = {"reused": ["existing Python environment", "authorized original sources"],
                                "created": ["compact SQLite/BM25 fixture", "owned temporary Redis", "private Chroma runtime", "evidence logs"],
                                "cleanup": "Owned Redis stopped; evidence and compact indexes retained for audit"}
        (output / "summary.json").write_bytes(canonical(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "execute"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        manifest_path, output = private_path(args.manifest), private_path(args.output)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        result = validate_manifest(manifest) if args.mode == "preflight" else execute(manifest, output)
        if args.mode == "preflight":
            output.mkdir(parents=True, exist_ok=True)
            (output / "preflight.json").write_bytes(canonical(result))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in ("PASS", "RECORDED_REQUIRES_EVIDENCE_REVIEW") else 2
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
