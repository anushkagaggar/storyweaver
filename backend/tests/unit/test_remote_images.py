"""Pictures through the `illustrate` Gradio endpoint: the worker's client and the drawing side (V3 step 4)."""

import dataclasses
import hashlib
import io
import json
import uuid
from types import SimpleNamespace
from typing import Any

import gradio as gr
import httpx
import pytest
from PIL import Image

from storyweaver.config import SDXL_DEFAULTS, Settings
from storyweaver.imaging import gpu_drawing
from storyweaver.imaging.generator import (
    ImageQuotaExhaustedError,
    ImageRejectedError,
    ImageUnavailableError,
)
from storyweaver.imaging.gpu_drawing import Drawing, zerogpu_outcome
from storyweaver.imaging.params import GenerationParams, cache_key
from storyweaver.imaging.remote import GradioIllustrator, IllustrateRequest, last_event
from storyweaver.imaging.sd15 import SD15Local
from storyweaver.imaging.sdxl import SDXL_IP_ADAPTER_MODEL
from storyweaver.services import build_illustrator, image_settings
from storyweaver.storage.blob import InMemoryBlobStore, image_key

pytestmark = pytest.mark.unit

STORY = uuid.UUID("00000000-0000-4000-8000-000000000001")
PARAMS = GenerationParams(
    prompt="children's picture book illustration, a small purple dragon",
    seed=8,
    model=str(SDXL_DEFAULTS["image_model"]),
    model_revision=str(SDXL_DEFAULTS["image_model_revision"]),
    width=1152,
    height=896,
    sampler="dpmpp_2m_karras",
    steps=25,
    guidance=6.0,
)
KEY = image_key(STORY, cache_key(PARAMS))


def _settings(**values: Any) -> Settings:
    return Settings(storyweaver_api_key="k" * 32, **values)  # type: ignore[arg-type]


# --- settings ---------------------------------------------------------------------------------------------


def test_sdxl_gets_its_pinned_model_and_its_own_ip_adapter() -> None:
    settings = _settings(image_engine="sdxl")
    assert settings.image_model_revision == SDXL_DEFAULTS["image_model_revision"] != "main"
    assert (settings.image_width, settings.image_height) == (1152, 896)
    assert image_settings(settings).reference_model == SDXL_IP_ADAPTER_MODEL
    assert "sdxl_vit-h" in SDXL_IP_ADAPTER_MODEL


def test_on_zerogpu_pictures_go_through_the_spaces_own_url() -> None:
    on_space = _settings(image_engine="sdxl", spaces_zero_gpu=True, space_host="someone-storyweaver.hf.space")
    assert on_space.image_gradio_url == "https://someone-storyweaver.hf.space"
    assert _settings(image_engine="sdxl", space_host="x.hf.space").image_gradio_url is None  # not ZeroGPU
    chosen = _settings(image_gradio_url="http://localhost:7860/ ")
    assert chosen.image_gradio_url == "http://localhost:7860"


def test_the_worker_uses_the_gradio_route_when_it_is_set() -> None:
    blobs: Any = object()
    illustrator = build_illustrator(
        _settings(image_engine="sdxl", image_gradio_url="http://localhost:7860", hf_token="hf_x"), blobs
    )
    assert isinstance(illustrator, GradioIllustrator) and illustrator.name == "gradio:sdxl"
    assert (
        build_illustrator(_settings(image_engine="placeholder", image_gradio_url="http://x"), blobs) is None
    )


# --- the wire format --------------------------------------------------------------------------------------


def test_a_request_round_trips_and_its_reference_must_match_the_hash() -> None:
    reference = b"page one png"
    params = dataclasses.replace(PARAMS, reference_sha256=hashlib.sha256(reference).hexdigest())
    request = IllustrateRequest(STORY, params, reference)
    assert IllustrateRequest.from_json(request.to_json()) == request
    assert request.key == image_key(STORY, cache_key(params))

    tampered = json.loads(request.to_json())
    tampered["params"]["reference_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="sha256"):
        IllustrateRequest.from_json(json.dumps(tampered))


def test_last_event_reads_a_gradio_stream() -> None:
    stream = 'event: heartbeat\ndata: null\n\nevent: complete\ndata: [{"ok": true}]\n\n'
    assert last_event(stream) == ("complete", '[{"ok": true}]')
    assert last_event("") is None


# --- the drawing side -------------------------------------------------------------------------------------


class FakePipeline:
    def __init__(self, *, nsfw: bool = False) -> None:
        self.calls = 0
        self.nsfw = nsfw

    def set_ip_adapter_scale(self, scale: float) -> None:
        pass

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        image = Image.new("RGB", (kwargs["width"], kwargs["height"]), (200, 180, 240))
        return SimpleNamespace(images=[image], nsfw_content_detected=[self.nsfw])


@pytest.fixture
def pipe(monkeypatch: pytest.MonkeyPatch) -> FakePipeline:
    fake = FakePipeline()
    engine = SD15Local(loader=lambda *_: fake, generator_for=lambda seed: seed, name="sdxl")
    monkeypatch.setattr(gpu_drawing, "_engine", engine)
    return fake


async def test_the_endpoint_draws_and_stores_the_picture_under_its_content_key(pipe: FakePipeline) -> None:
    blobs = InMemoryBlobStore()
    answer = await Drawing(lambda: blobs).illustrate(IllustrateRequest(STORY, PARAMS).to_json())

    assert answer["ok"] and answer["key"] == KEY and "png" not in answer
    assert blobs.objects[KEY][1] == "image/png" and pipe.calls == 1

    again = await Drawing(lambda: blobs).illustrate(IllustrateRequest(STORY, PARAMS).to_json())
    assert again["ok"] and pipe.calls == 1  # already stored: a retry draws nothing


async def test_the_endpoint_reports_rejections_and_bad_requests_without_storing(
    pipe: FakePipeline,
) -> None:
    blobs = InMemoryBlobStore()
    pipe.nsfw = True
    answer = await Drawing(lambda: blobs).illustrate(IllustrateRequest(STORY, PARAMS).to_json())
    assert answer["outcome"] == "rejected" and blobs.objects == {}

    assert (await Drawing(lambda: blobs).illustrate("{not json"))["outcome"] == "invalid"
    assert (await Drawing(lambda: None).illustrate("{}"))["outcome"] == "unavailable"


async def test_zerogpu_refusals_become_quota_or_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any) -> dict[str, Any]:
        raise gr.Error("You have exceeded your Pro ZeroGPU quota", title="ZeroGPU quota exceeded")

    monkeypatch.setattr(gpu_drawing, "draw_on_gpu", refuse)
    answer = await Drawing(InMemoryBlobStore).illustrate(IllustrateRequest(STORY, PARAMS).to_json())
    assert answer["outcome"] == "quota"
    assert zerogpu_outcome(gr.Error("busy", title="ZeroGPU queue timeout")) == "unavailable"


async def test_without_an_engine_the_endpoint_says_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_drawing, "_engine", None)
    answer = await Drawing(InMemoryBlobStore).illustrate(IllustrateRequest(STORY, PARAMS).to_json())
    assert answer["outcome"] == "unavailable"


# --- the worker's client, against the real drawing side ---------------------------------------------------


class FakeGradio:
    """Gradio's two-step HTTP API in front of `Drawing`, recording the headers each call carried."""

    def __init__(self, drawing: Drawing | None = None, answer: object = None, status: int = 200) -> None:
        self.drawing = drawing
        self.answer = answer
        self.status = status
        self.headers: list[httpx.Headers] = []
        self.jobs: dict[str, str] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        if self.status != 200:
            return httpx.Response(self.status, text="Space is sleeping")
        if request.method == "POST":
            assert request.url.path == "/gradio_api/call/illustrate"
            self.jobs["e1"] = json.loads(request.content)["data"][0]
            return httpx.Response(200, json={"event_id": "e1"})
        assert request.url.path == "/gradio_api/call/illustrate/e1"
        answer = await self.drawing.illustrate(self.jobs["e1"]) if self.drawing else self.answer
        if isinstance(answer, str):
            return httpx.Response(200, text=answer)
        return httpx.Response(200, text=f"event: complete\ndata: {json.dumps([answer])}\n\n")


def client(gradio: FakeGradio, hf_token: str | None = "hf_token") -> GradioIllustrator:  # noqa: S107
    return GradioIllustrator(
        "https://someone-storyweaver.hf.space/",
        "api-key",
        engine="sdxl",
        hf_token=hf_token,
        transport=httpx.MockTransport(gradio),
    )


async def test_the_worker_gets_a_stored_picture_and_zerogpu_charges_its_token(pipe: FakePipeline) -> None:
    blobs = InMemoryBlobStore()
    gradio = FakeGradio(Drawing(lambda: blobs))
    page_one = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 60)).save(page_one, format="PNG")
    reference = page_one.getvalue()
    params = dataclasses.replace(PARAMS, reference_sha256=hashlib.sha256(reference).hexdigest())
    key = image_key(STORY, cache_key(params))

    await client(gradio).illustrate(STORY, params, key, reference)

    assert key in blobs.objects and pipe.calls == 1
    assert all(
        h["x-api-key"] == "api-key" and h["authorization"] == "Bearer hf_token" for h in gradio.headers
    )
    without = FakeGradio(Drawing(lambda: blobs))
    await client(without, hf_token=None).illustrate(STORY, params, key, reference)
    assert "authorization" not in without.headers[0]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"ok": False, "outcome": "rejected", "detail": "flagged"}, ImageRejectedError),
        ({"ok": False, "outcome": "quota", "detail": "60s left"}, ImageQuotaExhaustedError),
        ({"ok": False, "outcome": "unavailable", "detail": "OOM"}, ImageUnavailableError),
        ({"ok": True, "outcome": "ok", "key": "stories/other.png"}, ImageUnavailableError),
        ("event: error\ndata: null\n\n", ImageUnavailableError),
        ("not a stream", ImageUnavailableError),
    ],
)
async def test_endpoint_answers_map_to_the_errors_the_job_handles(answer: object, error: type) -> None:
    with pytest.raises(error):
        await client(FakeGradio(answer=answer)).illustrate(STORY, PARAMS, KEY)


async def test_an_unreachable_space_is_worth_a_retry() -> None:
    with pytest.raises(ImageUnavailableError, match="unreachable"):
        await client(FakeGradio(status=503)).illustrate(STORY, PARAMS, KEY)


async def test_the_caller_cannot_choose_where_the_picture_is_stored() -> None:
    with pytest.raises(ValueError, match="key"):
        await client(FakeGradio(answer={})).illustrate(STORY, PARAMS, "stories/elsewhere.png")


# --- the route self-check -----------------------------------------------------------------------------------


def test_the_gpu_route_check_needs_the_gradio_route(
    client: Any, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from storyweaver.config import get_settings

    monkeypatch.setenv("IMAGE_GRADIO_URL", "")
    get_settings.cache_clear()
    assert client.get("/v1/gpu-route-check", headers=auth_headers).status_code == 404
    assert client.get("/v1/gpu-route-check").status_code == 401


@pytest.mark.parametrize(("answer", "ok"), [({"ok": True, "device": "H200"}, True), ({"ok": False}, False)])
def test_the_gpu_route_check_calls_this_spaces_own_gpu_check(
    client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    answer: dict[str, Any],
    ok: bool,
) -> None:
    from storyweaver.config import get_settings

    calls: list[str] = []

    async def call(self: GradioIllustrator, name: str, *data: object) -> object:
        calls.append(name)
        return answer

    monkeypatch.setenv("IMAGE_GRADIO_URL", "https://someone-storyweaver.hf.space")
    get_settings.cache_clear()
    monkeypatch.setattr(GradioIllustrator, "call", call)
    body = client.get("/v1/gpu-route-check", headers=auth_headers).json()
    assert calls == ["gpu_check"]
    assert (
        body["ok"] is ok
        and body["url"] == "https://someone-storyweaver.hf.space"
        and body["detail"] == answer
    )
