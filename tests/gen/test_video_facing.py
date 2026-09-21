# SPDX-License-Identifier: Apache-2.0
"""Side input correction is opt-in for standalone clips and never edits the source."""
import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageOps

from sprite_gen.gen import video

FIXTURE = Path(__file__).parents[1] / "fixtures" / "facing" / "left.png"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


class Api:
    def __init__(self, direction="left", failure=False):
        self.direction, self.failure = direction, failure
        self.calls = []

    def call(self, method, url, token, body):
        self.calls.append((method, url, token, body))
        if url.endswith("/responses"):
            if self.failure:
                return 403, {}
            return 200, {"model": "test-vision", "usage": {"input_tokens": 4},
                         "output": [{"type": "message", "content": [{"type": "output_text", "text": self.direction}]}]}
        if method == "POST":
            return 200, {"request_id": "test-id"}
        return 200, {"status": "done", "video": {"url": "https://vidgen.x.ai/test.mp4", "duration": 3}}


@pytest.mark.parametrize("fix", ["mirror", "none"])
@pytest.mark.parametrize("observed", ["left", "right", "front", "unknown"])
def test_side_input_is_inspected_once_before_upload(tmp_path, fix, observed):
    api = Api(observed)
    original = FIXTURE.read_bytes()
    request = video.VideoRequest(FIXTURE, "idle", tmp_path / "out.mp4", direction="side", facing_fix=fix)
    result = video.generate_video(request, credential=video.Credential("synthetic", "grok-login"),
                                  call=api.call, download=lambda *a: MP4)
    assert FIXTURE.read_bytes() == original
    assert len(api.calls) == 3 and api.calls[0][1].endswith("/responses")
    assert all(call[2] == "synthetic" for call in api.calls)
    body = api.calls[1][3]
    assert "facing right" in body["prompt"]
    report = result.to_dict()["facing"]
    assert report["direction"] == observed and report["usage"] == {"input_tokens": 4}
    mirrored = fix == "mirror" and observed == "left"
    assert report["action"] == ("mirror" if mirrored else "none")
    with Image.open(FIXTURE) as source:
        expected = ImageOps.mirror(source) if mirrored else source
        assert Image.open(result.image).tobytes() == expected.tobytes()
        # The actual upload, not just the copy on disk, carries the correction.
        url = body["image"]["url"]
        uploaded = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        assert uploaded.tobytes() == expected.tobytes()


@pytest.mark.parametrize("direction", [None, "front", "back"])
def test_non_side_video_keeps_prompt_and_has_no_inspection(tmp_path, direction):
    api = Api()
    result = video.generate_video(video.VideoRequest(FIXTURE, "original prompt", tmp_path / "out.mp4", direction=direction),
                                  credential=video.Credential("synthetic", "grok-login"), call=api.call, download=lambda *a: MP4)
    assert len(api.calls) == 2
    assert result.prompt == "original prompt" and "facing" not in result.to_dict()
    assert result.image == FIXTURE


def test_failed_video_inspection_is_reported_and_generation_continues(tmp_path):
    api = Api(failure=True)
    result = video.generate_video(video.VideoRequest(FIXTURE, "idle", tmp_path / "out.mp4", direction="side"),
                                  credential=video.Credential("synthetic", "grok-login"), call=api.call, download=lambda *a: MP4)
    assert result.facing["direction"] == "unknown" and "reason" in result.facing
    assert result.image.read_bytes() == FIXTURE.read_bytes()
    assert len(api.calls) == 3


def test_side_multi_input_refused_before_vision_or_video(tmp_path):
    api = Api()
    with pytest.raises(SystemExit, match="single --image"):
        video.generate_video(video.VideoRequest(FIXTURE, "idle", tmp_path / "out.mp4", direction="side", last_frame=FIXTURE),
                             credential=video.Credential("synthetic", "grok-login"), call=api.call)
    assert not api.calls


def test_video_cli_passes_side_policy(tmp_path, monkeypatch):
    api = Api()
    monkeypatch.setattr(video, "resolve_credential", lambda: video.Credential("synthetic", "grok-login"))
    monkeypatch.setattr(video, "http_json", api.call)
    monkeypatch.setattr(video, "http_download", lambda *a: MP4)
    report = tmp_path / "report.json"
    assert video.main(["--image", str(FIXTURE), "--prompt", "idle", "--out", str(tmp_path / "out.mp4"),
                       "--direction", "side", "--facing", "left", "--facing-fix", "none", "--report", str(report)]) == 0
    data = json.loads(report.read_text())
    assert data["facing"]["requested"] == "left" and "facing left" in data["prompt"]
