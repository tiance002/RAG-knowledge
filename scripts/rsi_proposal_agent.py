"""Tool-free SiliconFlow code proposer; credentials never enter the prompt or archive."""
import base64
import json
import os
from pathlib import Path
import subprocess

from rsi_candidates import canonical
from rsi_siliconflow_transport import MODEL, send

FIELDS = ("hypothesis", "change", "impact", "falsification", "old", "new")
INSTRUCTIONS = (
    "You are the code improvement agent for a controlled engineering experiment. "
    "Diagnose the observed failure in the supplied parent selector, considering the actual returned "
    "result as well as the selection process. Propose the smallest single general code edit that "
    "repairs the cause. Return a JSON object of six strings: hypothesis, change, impact, "
    "falsification, old, new. old and new are exact Python source substrings for one replacement "
    "in the function body. Preserve indentation. No imports, I/O, questions, case IDs or "
    "example-specific constants. Preserve independent seeds, order and source-verified duplicates "
    "policy. State the expected impact on actual delivery and a falsifying observation."
)


class ProposalAgent:
    def __init__(self, model):
        if model != MODEL:
            raise ValueError("Unexpected proposal model")

    def request(self, evidence):
        request = {"model": MODEL, "stream": False, "enable_thinking": False,
                   "temperature": 0, "max_tokens": 1200, "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": INSTRUCTIONS},
                                {"role": "user", "content": canonical(evidence).decode()}]}
        if len(canonical(request)) > 6000:
            raise ValueError("Refusing proposal above 6000 UTF-8 request bytes")
        return request

    def propose(self, request):
        body = canonical(request)
        key = os.getenv("SILICONFLOW_API_KEY")
        if key:
            return send(body, key)
        host = os.environ["RSI_SSH_TARGET"]
        if not host or host.startswith("-") or any(ch.isspace() for ch in host):
            raise ValueError("Invalid RSI_SSH_TARGET")
        relay = Path(__file__).with_name("rsi_siliconflow_transport.py").read_bytes()
        command = "python3 -B -c \"import base64;exec(base64.b64decode('" + base64.b64encode(relay).decode() + "'))\""
        ssh = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7", "-o", "IdentitiesOnly=yes",
               "-i", os.environ["RSI_SSH_IDENTITY"], host, command]
        proc = subprocess.run(ssh, input=body, capture_output=True, timeout=135, check=True)
        return json.loads(proc.stdout)
