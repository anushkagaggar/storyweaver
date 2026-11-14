"""V2 MCP additions through the real MCP protocol: `transcribe`, `narrate` and the `voices://list` resource.

The REST API is mocked with respx. `transcribe` is the one tool that reads a file off this machine, so the
tests also cover what it refuses to send.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from helpers import API, connect, text_of
from mcp.types import TextResourceContents

MP3 = b"ID3\x04fake-mp3-bytes"
VOICES = {
    "engines": {
        "orpheus": {
            "narrator": "hannah",
            "voices": [{"id": "hannah", "label": "Hannah (female)", "accent": "en-us"}],
        },
        "kokoro": {
            "narrator": "af_heart",
            "voices": [{"id": "af_heart", "label": "Heart (US female)", "accent": "en-us"}],
        },
    }
}


def files_in(directory: Path) -> list[Path]:
    return list(directory.iterdir())


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def problem(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


@respx.mock
async def test_transcribe_sends_the_recording_as_the_raw_body(tmp_path: Path) -> None:
    recording = tmp_path / "idea.wav"
    recording.write_bytes(b"RIFF....WAVEfake")
    reply = {
        "text": "a purple dragon",
        "engine": "groq",
        "model": "whisper-large-v3-turbo",
        "duration_seconds": 2.1,
    }
    async with connect() as session:
        route = respx.post(f"{API}/v1/speech/transcribe").mock(return_value=httpx.Response(200, json=reply))

        result = await session.call_tool("transcribe", {"audio_path": str(recording)})

        assert not result.isError
        assert result.structuredContent == reply
        request = route.calls.last.request
        assert request.content == b"RIFF....WAVEfake"
        assert request.headers["Content-Type"] == "audio/wav"
        assert request.headers["X-API-Key"] == "test-key"  # pragma: allowlist secret


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [("a.WEBM", "audio/webm"), ("a.m4a", "audio/mp4"), ("a.mp3", "audio/mpeg"), ("a.opus", "audio/ogg")],
)
@respx.mock
async def test_transcribe_picks_the_content_type_from_the_extension(
    tmp_path: Path, filename: str, content_type: str
) -> None:
    (tmp_path / filename).write_bytes(b"x")
    async with connect() as session:
        route = respx.post(f"{API}/v1/speech/transcribe").mock(
            return_value=httpx.Response(200, json={"text": "hi"})
        )

        await session.call_tool("transcribe", {"audio_path": str(tmp_path / filename)})

        assert route.calls.last.request.headers["Content-Type"] == content_type


async def test_transcribe_refuses_files_that_are_not_recordings(tmp_path: Path) -> None:
    """A prompt must not be able to make this tool upload a secret file."""
    secret = tmp_path / ".env"
    secret.write_text("STORYWEAVER_API_KEY=hunter2")  # pragma: allowlist secret
    async with connect() as session:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{API}/v1/speech/transcribe")
            result = await session.call_tool("transcribe", {"audio_path": str(secret)})

        assert result.isError and "must be a recording" in text_of(result)
        assert not route.called


async def test_transcribe_missing_unreadable_and_oversized_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "folder.wav").mkdir()
    big = tmp_path / "big.wav"
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr("client.MAX_AUDIO_BYTES", 50)
    async with connect() as session:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{API}/v1/speech/transcribe")
            missing = await session.call_tool("transcribe", {"audio_path": str(tmp_path / "nope.wav")})
            folder = await session.call_tool("transcribe", {"audio_path": str(tmp_path / "folder.wav")})
            oversized = await session.call_tool("transcribe", {"audio_path": str(big)})

        assert missing.isError and "No recording found" in text_of(missing)
        assert folder.isError and "can't be read" in text_of(folder)
        assert oversized.isError and "under 10 MB" in text_of(oversized)
        assert not route.called


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (httpx.Response(422, json=problem("audio_silent", "We couldn't hear anything.")), "couldn't hear"),
        (
            httpx.Response(
                422, json=problem("audio_too_long", "Please keep your story idea under 60 seconds.")
            ),
            "60 seconds",
        ),
        (
            httpx.Response(415, json=problem("unsupported_media_type", "Please send a voice recording.")),
            "voice recording",
        ),
        (
            httpx.Response(503, json=problem("stt_unavailable", "Voice input isn't working right now.")),
            "isn't working",
        ),
        (
            httpx.Response(
                429, headers={"retry-after": "20"}, json=problem("stt_busy", "Voice input is busy.")
            ),
            "20 seconds",
        ),
    ],
    ids=["silent", "too-long", "unsupported", "unavailable", "busy"],
)
@respx.mock
async def test_transcribe_shows_the_apis_parent_facing_messages(
    tmp_path: Path, response: httpx.Response, fragment: str
) -> None:
    (tmp_path / "idea.webm").write_bytes(b"x")
    async with connect() as session:
        respx.post(f"{API}/v1/speech/transcribe").mock(return_value=response)

        result = await session.call_tool("transcribe", {"audio_path": str(tmp_path / "idea.webm")})

        assert result.isError and fragment in text_of(result)


@respx.mock
async def test_narrate_saves_the_mp3_and_reports_where(tmp_path: Path) -> None:
    headers = {
        "content-type": "audio/mpeg",
        "x-tts-engine": "kokoro",
        "x-tts-voice": "bf_emma",
        "x-audio-duration": "1.80",
    }
    async with connect(audio_dir=tmp_path / "audio") as session:
        route = respx.post(f"{API}/v1/speech/narrate").mock(
            return_value=httpx.Response(200, content=MP3, headers=headers)
        )

        result = await session.call_tool("narrate", {"text": "Goodnight, little fox.", "voice": "bf_emma"})

        assert not result.isError
        content = result.structuredContent
        assert content is not None
        saved = Path(content["audio_path"])
        assert saved.parent == tmp_path / "audio" and saved.suffix == ".mp3"
        assert files_in(saved.parent) == [saved] and read_bytes(saved) == MP3
        assert content["audio_url"] == saved.as_uri()
        assert (content["engine"], content["voice"], content["duration_seconds"]) == (
            "kokoro",
            "bf_emma",
            1.8,
        )
        request = route.calls.last.request
        assert json.loads(request.content) == {"text": "Goodnight, little fox.", "voice": "bf_emma"}
        assert request.headers["X-API-Key"] == "test-key"  # pragma: allowlist secret


@respx.mock
async def test_narrate_without_a_voice_lets_the_api_choose(tmp_path: Path) -> None:
    headers = {"x-tts-engine": "orpheus", "x-tts-voice": "hannah", "x-audio-duration": "1"}
    async with connect(audio_dir=tmp_path) as session:
        route = respx.post(f"{API}/v1/speech/narrate").mock(
            return_value=httpx.Response(200, content=MP3, headers=headers)
        )

        result = await session.call_tool("narrate", {"text": "Hello."})

        assert not result.isError and result.structuredContent is not None
        assert result.structuredContent["voice"] == "hannah"
        assert json.loads(route.calls.last.request.content) == {"text": "Hello."}


@pytest.mark.parametrize("arguments", [{"text": ""}, {"text": "x" * 601}], ids=["empty", "too-long"])
async def test_narrate_validates_text_before_calling_the_api(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    async with connect(audio_dir=tmp_path) as session:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{API}/v1/speech/narrate")
            result = await session.call_tool("narrate", arguments)

        assert result.isError and "text" in text_of(result)
        assert not route.called
        assert not files_in(tmp_path)


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (
            httpx.Response(422, json=problem("unknown_voice", "That voice isn't available.")),
            "isn't available",
        ),
        (
            httpx.Response(
                429, headers={"retry-after": "33"}, json=problem("tts_busy", "That voice is busy.")
            ),
            "33 seconds",
        ),
        (
            httpx.Response(503, json=problem("tts_unavailable", "Reading aloud isn't working right now.")),
            "isn't working",
        ),
    ],
    ids=["unknown-voice", "busy", "unavailable"],
)
@respx.mock
async def test_narrate_errors_are_tool_errors_and_save_nothing(
    tmp_path: Path, response: httpx.Response, fragment: str
) -> None:
    async with connect(audio_dir=tmp_path) as session:
        respx.post(f"{API}/v1/speech/narrate").mock(return_value=response)

        result = await session.call_tool("narrate", {"text": "Hello.", "voice": "nobody"})

        assert result.isError and fragment in text_of(result)
        assert not files_in(tmp_path)


@respx.mock
async def test_voices_resource_returns_the_apis_catalogue() -> None:
    async with connect() as session:
        respx.get(f"{API}/v1/speech/voices").mock(return_value=httpx.Response(200, json=VOICES))

        resource = await session.read_resource("voices://list")  # type: ignore[arg-type]

        content = resource.contents[0]
        assert isinstance(content, TextResourceContents)
        assert content.mimeType == "application/json"
        assert json.loads(content.text) == VOICES
