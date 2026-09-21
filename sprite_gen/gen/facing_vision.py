# SPDX-License-Identifier: Apache-2.0
"""Single-call vision transports for the existing generation providers.

REST input/output contract: OpenAI and xAI Responses image understanding docs.
No generated image is sent to a different provider or authentication route.
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from pathlib import Path

from .base import GEN_TIMEOUT_SECONDS, provider_binary, provider_subprocess_env
from .facing import INSPECTION_PROMPT

OPENAI_MODEL = "gpt-4.1-mini"
GROK_MODEL = "grok-4.6"


def request_body(path: Path, model: str) -> dict:
    return {"model": model, "store": False, "max_output_tokens": 256,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": INSPECTION_PROMPT},
                {"type": "input_image", "image_url": "data:image/png;base64," +
                 base64.b64encode(path.read_bytes()).decode("ascii"), "detail": "low"},
            ]}]}


def response_text(status: int, reply) -> tuple[str, dict]:
    if status != 200:
        raise SystemExit(f"facing-vision: HTTP {status}; no retry or fallback")
    if not isinstance(reply, dict):
        raise ValueError("invalid vision envelope")
    text = "".join(part["text"] for item in reply.get("output", [])
                   if item.get("type") == "message"
                   for part in item.get("content", []) if part.get("type") == "output_text")
    return text, {"model": reply.get("model"), "usage": reply.get("usage"),
                  "response_status": reply.get("status")}


def codex_inspect(path: Path, workdir: Path) -> tuple[str, dict]:
    # Isolated cwd and ephemeral session: no project instructions, no stale
    # output reuse and no persistent vision rollouts. Uses the existing login.
    with tempfile.TemporaryDirectory(prefix="facing-", dir=workdir) as directory:
        output = Path(directory) / "answer.txt"
        command = [provider_binary("codex"), "exec", "--sandbox", "read-only",
                   "--skip-git-repo-check", "--ephemeral", "--color", "never",
                   "-C", directory, "-i", str(path.resolve()), "-o", str(output), "-"]
        completed = subprocess.run(command, input=INSPECTION_PROMPT, capture_output=True,
                                   text=True, encoding="utf-8", env=provider_subprocess_env(),
                                   timeout=GEN_TIMEOUT_SECONDS)
        if completed.returncode:
            raise SystemExit(f"facing-vision: codex exited {completed.returncode}")
        return output.read_text(encoding="utf-8"), {"transport": "codex-exec", "auth_source": "codex-login"}


def grok_inspect(path: Path, *, credential=None, call=None) -> tuple[str, dict]:
    from . import xai
    from .base import announce_api_billing
    credential = credential or xai.resolve_credential()
    if credential.source == xai.AUTH_SOURCE_API_KEY:
        announce_api_billing("grok", xai.AUTH_ENV, " (facing vision check).")
    status, reply = (call or xai.http_json)("POST", xai.API_BASE + "/responses", credential.token,
                                          request_body(path, GROK_MODEL))
    text, metadata = response_text(status, reply)
    return text, {**metadata, "auth_source": credential.source, "transport": "xai-api"}
