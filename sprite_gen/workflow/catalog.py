# SPDX-License-Identifier: Apache-2.0
"""The two user journeys and their selectable fields, declared once."""
from sprite_gen.gen import PROVIDERS

MOTION_METHODS = {
    "gpt-rows": {"label": "지피티 이미지 스프라이트", "provider": "codex", "doc": "docs/atlas-workflow.md",
                 "steps": ["prepare", "gen-set", "extract", "compose-atlas", "compose-gif"]},
    "grok-video": {"label": "그록 영상", "provider": "grok", "doc": "docs/video-pipeline.md",
                   "steps": ["video-set"]},
}
# One label per registered provider. A provider with no label here is a bug, not
# a row to drop quietly: the mapping is checked rather than zipped positionally,
# so registering a backend in `gen.PROVIDERS` without naming it fails at import.
PROVIDER_LABELS = {
    "codex": "지피티 (ChatGPT 로그인)",
    "grok": "그록",
    "openai": "지피티 (API 키)",
}
_unlabelled = [provider for provider in PROVIDERS if provider not in PROVIDER_LABELS]
if _unlabelled:
    raise RuntimeError(f"workflow catalog: no label for provider(s) {', '.join(_unlabelled)}")

FIELDS = {
    "image_provider": {"options": {provider: PROVIDER_LABELS[provider] for provider in PROVIDERS},
                       "question": "이미지를 무엇으로 만들까요?"},
    "motion_method": {"options": {k: v["label"] for k, v in MOTION_METHODS.items()},
                      "question": "동작을 그록 영상으로 만들까요, 지피티 이미지 스프라이트로 만들까요?"},
    "curation": {"options": {"open": "열기", "skip": "열지 않기"},
                 "question": "큐레이션뷰에서 결과를 확인하고 골라볼까요?"},
}
FLOWS = {
    "sprite": {"label": "스프라이트 만들기", "fields": ("image_provider", "motion_method", "curation")},
    "image": {"label": "이미지 만들기", "fields": ("image_provider", "curation")},
}


def validate_choices(kind: str, choices: dict) -> dict:
    if kind not in FLOWS:
        raise ValueError(f"unknown workflow: {kind}")
    if not isinstance(choices, dict):
        raise ValueError("workflow choices must be an object")
    for key, value in choices.items():
        if key not in FLOWS[kind]["fields"] or not isinstance(value, str) or value not in FIELDS[key]["options"]:
            raise ValueError(f"invalid {kind} choice: {key}")
    return dict(choices)
