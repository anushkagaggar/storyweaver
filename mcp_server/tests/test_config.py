"""Where the MCP server gets its API key and URL.

This matters because the MCP SDK spawns a stdio server with only an allowlist of environment variables
(PATH, TEMP, ...), so under `mcp dev` a shell `export` never reaches us and the .env fallback is the only
thing that works. Claude Desktop's `env` block does reach us, so the environment must still win.
"""

from pathlib import Path

import pytest

from client import DEFAULT_API_URL, ApiConfig, read_env_file

KEY = "STORYWEAVER_API_KEY"
URL = "STORYWEAVER_API_URL"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (KEY, URL, "STORYWEAVER_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)


def test_the_environment_wins_over_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY}=from-file\n{URL}=http://from-file\n")  # pragma: allowlist secret
    monkeypatch.setenv("STORYWEAVER_ENV_FILE", str(env_file))
    monkeypatch.setenv(KEY, "from-environment")  # pragma: allowlist secret

    config = ApiConfig.from_env()

    assert config.api_key == "from-environment"  # pragma: allowlist secret
    assert config.base_url == "http://from-file"  # not set in the environment, so the file supplies it


def test_falls_back_to_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY}=from-file\n")  # pragma: allowlist secret
    monkeypatch.setenv("STORYWEAVER_ENV_FILE", str(env_file))

    config = ApiConfig.from_env()

    assert config.api_key == "from-file"  # pragma: allowlist secret
    assert config.base_url == DEFAULT_API_URL


def test_a_missing_key_explains_the_mcp_dev_trap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORYWEAVER_ENV_FILE", str(tmp_path / "absent.env"))

    with pytest.raises(RuntimeError) as raised:
        ApiConfig.from_env()

    message = str(raised.value)
    assert "absent.env" in message and "shell" in message


def test_a_trailing_slash_is_dropped_so_paths_do_not_double_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORYWEAVER_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv(KEY, "k")  # pragma: allowlist secret
    monkeypatch.setenv(URL, "https://example.hf.space/")

    assert ApiConfig.from_env().base_url == "https://example.hf.space"


def test_env_file_parsing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "  ",
                "PLAIN=value",
                'QUOTED="quoted value"',
                "SINGLE='single'",
                "export EXPORTED=exported",
                "SPACED =  padded  ",
                "EMPTY=",
                "no_equals_sign",
                "URL=http://localhost:7860/path?a=b",  # only the first '=' splits
            ]
        )
    )

    assert read_env_file(env_file) == {
        "PLAIN": "value",
        "QUOTED": "quoted value",
        "SINGLE": "single",
        "EXPORTED": "exported",
        "SPACED": "padded",
        "EMPTY": "",
        "URL": "http://localhost:7860/path?a=b",
    }


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert read_env_file(tmp_path / "nope.env") == {}
    assert read_env_file(tmp_path) == {}  # a directory, not a file
