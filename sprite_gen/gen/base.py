# SPDX-License-Identifier: Apache-2.0
"""Shared contract for sprite-gen image generation providers.

One generation call = prompt (+ optional reference images) -> one verified raw
PNG on disk. Providers own the model call and its timing; the orchestrator in
`sprite_gen.gen` owns the optional transparent chroma post-process and the
report. Truth is always the decoded PNG bytes on disk, never a model-reported
path or a "done" string (No Silent Fallback).
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, UnidentifiedImageError

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Transparency strategy — a capability each provider declares exactly once
# (`Provider.transparency`). The orchestrator reads it; nothing else decides.
#   native — the model returns a genuinely transparent PNG (alpha channel) when
#            the prompt asks for a transparent background; the raw alpha is
#            measured and published (codex `image_gen`, 2026-09-08 실측).
#   chroma — the model cannot return alpha; generate on a chroma key and key it
#            out deterministically downstream (grok Imagine: API/CLI 모두 JPEG).
TRANSPARENCY_NATIVE = "native"
TRANSPARENCY_CHROMA = "chroma"
TRANSPARENCY_STRATEGIES = (TRANSPARENCY_NATIVE, TRANSPARENCY_CHROMA)

# Quality is a per-provider capability over one shared vocabulary. This tuple is
# the `--quality` surface (the union); each provider declares the subset it can
# honour and fails loud on a name outside it, so a paid-for level is never
# quietly downgraded to whatever the backend felt like (No Silent Fallback).
QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")

# Output resolution — the second billed knob, over one shared vocabulary of grok
# Imagine's output-size tiers, which it prices together with `quality`. The names
# are tiers, not pixel counts: `1.5k` rendered 1408x1408 at 1:1, not 1536
# (2026-09-20 실측 on the XAI_API_KEY route), so nothing here derives a size from
# the name — the tier goes to the service verbatim and the service sizes. A
# provider that sizes differently (openai derives a gpt-image `size` from
# `--aspect-ratio`) refuses the flag rather than accepting money for a resolution
# it will not deliver. Distinct from the video RESOLUTIONS
# (`480p`/`720p`/`1080p`), which names a frame height.
RESOLUTIONS = ("1k", "1.5k", "2k")

# Child provider processes are independent execution contexts. They must not
# inherit parent orchestration identity or lifecycle controls, while ordinary
# variables such as PATH remain available. Suffix matching keeps the contract
# provider-neutral and covers any orchestration namespace.
_ORCHESTRATOR_SESSION_ENV_SUFFIXES = (
    "_RUNTIME_ENDPOINT_ID",
    "_MEMBER_ID",
    "_PROJECT_ID",
    "_PLAN_EXIT_GATE",
    "_STUDIO_PORT",
)


# 생성 서브프로세스 하드 타임아웃 — provider 스트림이 드물게 무출력으로 매달린다
# (회귀 2026-07-19: 15행 배치 중 1행의 codex exec 이 1시간 38분 무출력 — 같은 env
# 로 14행이 성공했으니 세션/훅 충돌이 아니라 provider 측 산발 스톨. 킬 외엔 답이
# 없으므로 바운드가 유일한 방어). 기본 180초 (maintainer 확정 2026-07-19; 실측 정상 최장 129초).
GEN_TIMEOUT_SECONDS = int(os.environ.get("SPRITE_GEN_GEN_TIMEOUT_SECONDS", "180"))


class GenTimeoutError(SystemExit):
    """생성 서브프로세스가 GEN_TIMEOUT_SECONDS 안에 끝나지 않아 킬됨 (관측 가능)."""


def provider_subprocess_env() -> dict[str, str]:
    """Environment for a headless generation subprocess.

    The parent environment minus known orchestrator session env families, so a
    spawned engine (`codex exec`) is a clean standalone process — it
    neither impersonates the spawning agent nor gets strangled by the
    orchestrator's hooks. SSoT for every provider's `subprocess.run` env —
    providers must not spawn with the inherited env directly.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.endswith(_ORCHESTRATOR_SESSION_ENV_SUFFIXES)
    }
    return env


def provider_binary(name: str) -> str:
    """Resolve a provider CLI through PATH, including Windows PATHEXT shims.

    npm installs provider CLIs as ``.cmd`` shims on Windows. ``CreateProcess``
    does not resolve those from a bare name, while ``shutil.which`` does. A
    missing command stays bare so the provider spawn still owns the observable
    failure instead of introducing a second availability answer here.
    """
    return shutil.which(name) or name


def announce_api_billing(provider: str, env_name: str, detail: str = "") -> None:
    """Say on stderr, before the request leaves, that this call bills API credit.

    sprite-gen is a subscription-first tool: codex runs on a ChatGPT login and
    grok prefers its Grok login. A route that instead spends metered API credit
    is never silent about it, because the person paying only finds out otherwise
    on the invoice (수홍 2026-09-20, 구독 우선 불변식 5).
    """
    print(
        f"[gen] {provider}: running on {env_name} — this is a per-call API charge, "
        f"not a subscription{detail}",
        file=sys.stderr,
    )


def verify_png(path: Path) -> int:
    """Return the PNG byte count, or raise SystemExit if it is missing/not a PNG."""
    if not path.is_file():
        raise SystemExit(f"gen: expected a generated PNG at {path}, but no file was written")
    data = path.read_bytes()
    if data[:8] != PNG_MAGIC:
        raise SystemExit(f"gen: file at {path} is not a PNG (magic mismatch) — refusing to claim success")
    return len(data)


@dataclass
class GenRequest:
    """A single image generation request."""

    prompt: str
    raw: Path  # provider writes the generated PNG (chroma background included) here
    refs: list[Path] = field(default_factory=list)
    model: str | None = None
    aspect_ratio: str | None = None  # grok honours this; openai maps it to a size; codex ignores it
    # Rendering effort/fidelity level, from QUALITIES. None = the provider's own
    # default; a level the provider does not support is an error, not a downgrade.
    quality: str | None = None
    # Output resolution, from RESOLUTIONS. None = the provider's own default
    # (grok: `1k`); a provider that cannot size its output this way is an error,
    # not a downgrade.
    resolution: str | None = None
    # Ask the model for a genuinely transparent background (alpha channel). Only
    # legal for a provider whose `transparency` is `native`; the orchestrator gates
    # it and the provider carries the request into its transport prompt.
    native_alpha: bool = False


def publish_png(data: bytes, path: Path, *, label: str) -> None:
    """Publish decoded provider image bytes as a verified PNG at `path`.

    One writer for every provider: decode first (a corrupt or non-image payload
    fails before anything is published), re-encode only when the payload is not
    already a PNG — grok Imagine answers with JPEG — then verify the bytes and
    swap them in atomically. Nothing is resized. A failure leaves whatever was at
    `path` untouched, so a stale raw is never reused as a result.
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            if source.format == "PNG":
                png = data
            else:
                buffer = io.BytesIO()
                source.convert("RGBA" if "A" in source.getbands() else "RGB").save(buffer, format="PNG")
                png = buffer.getvalue()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise SystemExit(f"{label}: response image is invalid; nothing published") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".png", delete=False) as tmp:
        temp = Path(tmp.name)
    try:
        temp.write_bytes(png)
        verify_png(temp)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@dataclass
class ProviderRun:
    """What a provider reports after writing `request.raw`."""

    provider: str
    elapsed_seconds: float
    model: str | None = None
    session_id: str | None = None  # codex rollout session id, when applicable
    extra: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    """A generation backend. `generate` must write a verified PNG to `request.raw`."""

    name: str
    transparency: str  # one of TRANSPARENCY_STRATEGIES — declared once per provider

    def generate(self, request: GenRequest, workdir: Path) -> ProviderRun: ...


@dataclass
class GenResult:
    """Full outcome of one `sprite-gen gen` invocation."""

    provider: str
    prompt: str
    out: Path
    raw: Path
    raw_bytes: int
    elapsed_seconds: float
    model: str | None = None
    session_id: str | None = None
    refs: list[Path] = field(default_factory=list)
    transparent: bool = False
    # Which transparency strategy produced `out` plus its measured stats
    # ({"strategy": "native"|"chroma", ...stats}); None when not transparent.
    alpha: dict[str, Any] | None = None
    # Chroma-key stats — populated only when the strategy was `chroma` (kept as
    # its own field so existing report readers keep working).
    chroma: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "sprite-gen-image-report",
            "provider": self.provider,
            "prompt": self.prompt,
            "out": str(self.out),
            "raw": str(self.raw),
            "raw_bytes": self.raw_bytes,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "model": self.model,
            "session_id": self.session_id,
            "refs": [str(ref) for ref in self.refs],
            "transparent": self.transparent,
            "alpha": self.alpha,
            "chroma": self.chroma,
            **({"extra": self.extra} if self.extra else {}),
        }
