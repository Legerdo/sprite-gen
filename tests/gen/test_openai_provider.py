# SPDX-License-Identifier: Apache-2.0
"""OpenAI Images API contract, offline: no key, no network, no codex subprocess.

The paid seam is the request body, so that is what is pinned here: which endpoint
is called, every field that decides what is billed (model / quality / size /
background / output_format), how the base64 answer becomes a verified PNG, and
which failures must never publish, retry or leak the key.
"""
import base64
import io
import json
import subprocess
import urllib.error

import pytest
from PIL import Image

from sprite_gen import gen
from sprite_gen.gen import openai_provider as openai
from sprite_gen.gen.base import GenRequest
from sprite_gen.workflow import access
from sprite_gen.workflow.catalog import FIELDS, PROVIDER_LABELS

KEY = "synthetic-secret"


def encoded_image(fmt="PNG", *, alpha=False, key=False):
    """A 12x8 fixture: a 4x4 subject block on a plain, transparent or magenta field."""
    if alpha:
        image, subject = Image.new("RGBA", (12, 8), (0, 0, 0, 0)), (20, 90, 200, 255)
    else:
        image, subject = Image.new("RGB", (12, 8), (255, 0, 255) if key else (20, 90, 200)), (20, 90, 200)
    for x in range(4, 8):
        for y in range(2, 6):
            image.putpixel((x, y), subject)
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no provider subprocess may be spawned"))
    state = {"status": 200, "body": {"data": [{"b64_json": encoded_image()}],
                                     "usage": {"input_tokens": 12, "output_tokens": 340}}, "calls": []}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @property
        def status(self):
            return state["status"]

        def read(self):
            return json.dumps(state["body"]).encode()

    def open_request(request, *, timeout):
        state["calls"].append((request, timeout))
        if state["status"] == 200:
            return Response()
        raise urllib.error.HTTPError(request.full_url, state["status"], "error", {},
                                     io.BytesIO(json.dumps(state["body"]).encode()))

    monkeypatch.setattr(openai.urllib.request, "urlopen", open_request)
    return state


def _body(request):
    return json.loads(request.data)


def test_generation_sends_the_billed_fields_and_publishes_a_png(tmp_path, api):
    result = gen.generate_image("openai", "a blue square", tmp_path / "out.png")
    request, timeout = api["calls"][0]
    assert request.full_url == "https://api.openai.com/v1/images/generations"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert request.get_header("Content-type") == "application/json"
    assert _body(request) == {"model": openai.DEFAULT_MODEL, "prompt": "a blue square", "n": 1,
                              "size": "1024x1024", "quality": "auto",
                              "output_format": "png", "background": "opaque"}
    assert timeout == openai.GEN_TIMEOUT_SECONDS
    # `response_format` is rejected for gpt-image models; the answer is always b64.
    assert "response_format" not in _body(request)
    with Image.open(result.out) as image:
        assert image.format == "PNG" and image.size == (12, 8)
    report = result.to_dict()
    assert report["provider"] == "openai" and report["model"] == openai.DEFAULT_MODEL
    assert report["extra"]["transport"] == "openai-api"
    assert report["extra"]["auth_source"] == "OPENAI_API_KEY"
    assert report["extra"]["usage"] == {"input_tokens": 12, "output_tokens": 340}
    assert KEY not in json.dumps(report)


@pytest.mark.parametrize("quality", openai.SUPPORTED_QUALITIES)
def test_every_quality_level_reaches_the_request_verbatim(tmp_path, api, quality):
    gen.generate_image("openai", "x", tmp_path / f"{quality}.png", quality=quality)
    assert _body(api["calls"][0][0])["quality"] == quality


@pytest.mark.parametrize("ratio,size", sorted(openai.SIZES.items()))
def test_aspect_ratio_maps_to_a_legal_gpt_image_size(tmp_path, api, ratio, size):
    gen.generate_image("openai", "x", tmp_path / "out.png", aspect_ratio=ratio)
    assert _body(api["calls"][0][0])["size"] == size
    if size == "auto":
        return
    width, height = (int(side) for side in size.split("x"))
    # The documented custom-size constraints, checked on the table itself.
    assert width % 16 == 0 and height % 16 == 0
    assert max(width, height) <= 3840
    assert 1 / 3 <= width / height <= 3
    assert 655_360 <= width * height <= 8_294_400


def test_model_and_quality_overrides_are_not_second_guessed(tmp_path, api):
    gen.generate_image("openai", "x", tmp_path / "out.png", model="gpt-image-2.5-sunburst", quality="max")
    body = _body(api["calls"][0][0])
    assert body["model"] == "gpt-image-2.5-sunburst" and body["quality"] == "max"


def test_transparent_asks_for_a_transparent_background_and_keeps_the_alpha(tmp_path, api):
    api["body"] = {"data": [{"b64_json": encoded_image(alpha=True)}]}
    result = gen.generate_image("openai", "a blue square", tmp_path / "alpha.png", transparent=True)
    body = _body(api["calls"][0][0])
    assert body["background"] == "transparent" and body["output_format"] == "png"
    assert result.alpha["strategy"] == "native" and result.alpha["alpha_zero_pct"] > 0
    with Image.open(result.out) as image:
        assert image.getpixel((0, 0)) == (0, 0, 0, 0)
        assert image.getpixel((6, 4))[3] == 255


def test_reference_edit_posts_multipart_images_in_order(tmp_path, api):
    refs = []
    for index in range(3):
        path = tmp_path / f"ref-{index}.png"
        Image.new("RGB", (12, 8), (index * 40, 0, 0)).save(path)
        refs.append(path)
    gen.generate_image("openai", "combine these", tmp_path / "edit.png", refs=refs, quality="high")
    request, _ = api["calls"][0]
    assert request.full_url == "https://api.openai.com/v1/images/edits"
    content_type = request.get_header("Content-type")
    assert content_type.startswith("multipart/form-data; boundary=")
    payload = request.data
    assert payload.count(b'name="image[]"') == len(refs)
    assert b'name="quality"\r\n\r\nhigh\r\n' in payload
    assert b'name="model"\r\n\r\n' + openai.DEFAULT_MODEL.encode() in payload
    positions = [payload.index(ref.read_bytes()) for ref in refs]
    assert positions == sorted(positions), "reference images are read positionally; order must survive"


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_api_failure_keeps_existing_output_and_does_not_retry(tmp_path, api, status):
    api.update(status=status, body={"error": {"message": f"{KEY} https://signed.invalid/private"}})
    out = tmp_path / "existing.png"
    out.write_bytes(b"existing")
    with pytest.raises(SystemExit, match=f"HTTP {status}") as error:
        gen.generate_image("openai", "x", out)
    assert KEY not in str(error.value)
    assert "signed.invalid" not in str(error.value)
    assert out.read_bytes() == b"existing"
    assert len(api["calls"]) == 1


@pytest.mark.parametrize("body", [{}, {"data": []}, {"data": [None]}, {"data": [{}, {}]},
                                  {"data": [{"b64_json": "???"}]},
                                  {"data": [{"b64_json": base64.b64encode(b"invalid image").decode()}]},
                                  {"data": [{"url": "https://signed.invalid/private"}]}])
def test_invalid_response_never_reuses_stale_raw(tmp_path, api, body):
    api["body"] = body
    raw = tmp_path / "raw.png"
    raw.write_bytes(b"previous raw")
    with pytest.raises(SystemExit):
        openai.OpenAIProvider().generate(GenRequest("x", raw), tmp_path)
    assert raw.read_bytes() == b"previous raw"
    assert len(api["calls"]) == 1
    assert list(tmp_path.iterdir()) == [raw]


@pytest.mark.parametrize("value", [None, ""])
def test_missing_key_names_the_variable_and_never_falls_back(tmp_path, api, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OPENAI_API_KEY")
    else:
        monkeypatch.setenv("OPENAI_API_KEY", value)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY") as error:
        gen.generate_image("openai", "x", tmp_path / "out.png")
    assert "codex" in str(error.value)  # names the login it will NOT silently use
    assert api["calls"] == []
    assert not (tmp_path / "out.png").exists()


def test_invalid_request_fails_before_any_billable_call(tmp_path, api):
    bad_ref = tmp_path / "ref.png"
    bad_ref.write_bytes(b"invalid")
    good_ref = tmp_path / "good.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(good_ref)
    for options in ({"prompt": "   "}, {"refs": [bad_ref]}, {"refs": [good_ref] * (openai.MAX_REFS + 1)},
                    {"aspect_ratio": "19.5:9"}, {"quality": "ultra"}):
        prompt = options.pop("prompt", "x")
        with pytest.raises(SystemExit):
            openai.OpenAIProvider().generate(GenRequest(prompt, tmp_path / "raw.png", **options), tmp_path)
    assert api["calls"] == []


def test_timeout_does_not_repeat_billable_request(tmp_path, api, monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(args)
        raise TimeoutError()

    monkeypatch.setattr(openai.urllib.request, "urlopen", timeout)
    with pytest.raises(SystemExit, match="no automatic retry"):
        gen.generate_image("openai", "x", tmp_path / "out.png")
    assert len(calls) == 1


def test_native_alpha_is_a_declared_capability(tmp_path, api):
    assert openai.OpenAIProvider().transparency == "native"
    # Forcing chroma stays legal (a prompt may already carry a key background).
    api["body"] = {"data": [{"b64_json": encoded_image(key=True)}]}
    result = gen.generate_image("openai", "blue on magenta", tmp_path / "keyed.png",
                                transparent=True, alpha_mode="chroma")
    assert result.alpha["strategy"] == "chroma"
    assert _body(api["calls"][0][0])["background"] == "opaque"


def test_provider_is_registered_everywhere_a_provider_must_be(api):
    assert "openai" in gen.PROVIDERS
    assert isinstance(gen._make_provider("openai", keep_session=False), openai.OpenAIProvider)
    # The workflow surface must name every registered backend, not the first two.
    assert set(FIELDS["image_provider"]["options"]) == set(gen.PROVIDERS) == set(PROVIDER_LABELS)
    assert access.probe_access("openai") == {"provider": "openai", "login": "ready", "subscription": "unknown",
                                             "quota": "unknown", "billing": "api-credit",
                                             "reason": "OpenAI images will use OPENAI_API_KEY and separate "
                                                       "API credit; confirm this billing choice."}


def test_cli_accepts_provider_and_quality(tmp_path, api):
    report = tmp_path / "report.json"
    assert gen.main(["--provider", "openai", "--prompt", "x", "--out", str(tmp_path / "cli.png"),
                     "--quality", "low", "--aspect-ratio", "16:9", "--report", str(report)]) == 0
    body = _body(api["calls"][0][0])
    assert body["quality"] == "low" and body["size"] == "1536x864"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["provider"] == "openai" and payload["provider_resolved_from"] == "explicit"
