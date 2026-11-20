# StoryWeaver MCP server

A thin MCP adapter over the StoryWeaver REST API (plan §6.7). It holds no business logic: every tool calls
`/v1`. V1 runs over **stdio** (Claude Desktop, MCP Inspector); V9 adds Streamable HTTP on the Space.

| Kind | Name | Calls |
|---|---|---|
| tool | `create_story(prompt, age_band="4-6", num_scenes=5)` | `POST /v1/stories` |
| tool | `get_story(story_id)` | `GET /v1/stories/{id}` |
| tool | `get_job_status(job_id)` | `GET /v1/jobs/{id}` |
| tool | `transcribe(audio_path)` (V2) | `POST /v1/speech/transcribe` |
| tool | `narrate(text, voice?)` (V2) | `POST /v1/speech/narrate` |
| tool | `illustrate(prompt, story_id?, character_id?)` (V3) | `POST /v1/pictures`, then `GET /v1/stories/{id}` |
| resource | `story://{story_id}` | `GET /v1/stories/{id}` |
| resource | `voices://list` (V2) | `GET /v1/speech/voices` |
| prompt | `bedtime_story(favourite_animal, theme)` | none |

**Speech tools (V2)**

- `transcribe` reads a recording from this computer (`.webm .ogg .opus .mp4 .m4a .aac .mp3 .wav .flac`, up to
  10 MB and 60 s) and sends it as the raw request body. Any other file type is refused before anything is read,
  so a prompt can't make it upload, say, a `.env`. The audio is not kept or logged, on either side.
- `narrate` reads up to 600 characters aloud and **saves the MP3 on this computer**; it returns `audio_path`
  and a `file://` `audio_url`, plus the engine and voice used. Files go to `STORYWEAVER_AUDIO_DIR` (default:
  `storyweaver-audio` in the system temp folder) and are never deleted by the server. With no `voice`, the API
  uses the narrator of whichever engine has quota (Orpheus, else Kokoro). An Orpheus voice with no quota left
  returns a "busy, try again in N seconds" error; it never silently switches voice.

**Picture tool (V3)**

- `illustrate` draws one picture-book picture from words like "draw a purple dragon" (the API takes out the
  request words and runs the same safety checks as a story idea), **waits for it** (about 10-30 s; up to
  2 minutes, then it returns the `story_id` to check later with `get_story`) and returns the picture itself
  plus `{story_id, title, image_status, picture_path, note}`. The PNG is also saved on this computer, in
  `STORYWEAVER_PICTURE_DIR` (default: `storyweaver-pictures` in the system temp folder).
- `story_id` (+ optional `character_id`, from `get_story`'s characters) is the plan's `character_ref`: draw a
  character from one of your stories ("Pip at the beach"). The API keeps its look, a seed near its own, and
  uses the story's page-1 picture as the reference when that page shows it.
- The picture is downloaded from its short-lived storage link with a separate HTTP client, so the API key is
  never sent anywhere but the API. A picture is a one-page story: `get_story` reads it, and it expires with
  the parent's stories.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `STORYWEAVER_API_URL` | `http://localhost:7860` | The local Docker API; the Space URL once V1 is deployed |
| `STORYWEAVER_API_KEY` | (required) | The same key as the backend's `STORYWEAVER_API_KEY` |
| `STORYWEAVER_AUDIO_DIR` | system temp / `storyweaver-audio` | Where `narrate` saves MP3s |
| `STORYWEAVER_PICTURE_DIR` | system temp / `storyweaver-pictures` | Where `illustrate` saves PNGs |
| `STORYWEAVER_ENV_FILE` | `mcp_server/.env`, then `../.env` | Where to read the two settings above from |

**The API key and URL also fall back to a `.env` file**, so the repo-root `.env` (the one the Docker stack
and the smoke script use) is picked up with no extra setup. Environment variables take precedence.

That fallback is not a convenience, it's a necessity: **the MCP SDK spawns a stdio server with only an
allowlist of environment variables** (`PATH`, `TEMP`, `APPDATA`, ... — `DEFAULT_INHERITED_ENV_VARS` in the
TypeScript SDK), so anything exported in your shell is dropped before the server starts. Setting
`STORYWEAVER_API_KEY` in PowerShell and then running `mcp dev` cannot work. Claude Desktop's `env` block is
passed explicitly, so that does reach the server.

When the server exits at startup the Inspector only shows `Remote connect failed`, which can look like a
CORS or origin problem. The real traceback is in the Inspector's console pane (`Export` writes it to JSON).

## Commands (from this folder)

```powershell
uv sync                     # install (you run this)
uv run pytest               # tests: real MCP protocol in memory, REST API mocked
uv run mcp dev server.py    # MCP Inspector in the browser (needs Node/npx; reads ../.env, see above)
uv run server.py            # stdio server (what Claude Desktop starts)
```

## Claude Desktop

Settings → Developer → Edit Config, then add (use your own key; this file stays on your PC):

```json
{
  "mcpServers": {
    "storyweaver": {
      "command": "uv",
      "args": ["--directory", "D:\\lenovo\\work\\storyweaver\\mcp_server", "run", "server.py"],
      "env": {
        "STORYWEAVER_API_URL": "http://localhost:7860",
        "STORYWEAVER_API_KEY": "<your local STORYWEAVER_API_KEY>"
      }
    }
  }
}
```

Restart Claude Desktop; the tools appear under the plug icon. Try the `bedtime_story` prompt, or ask
"Make me a bedtime story about a shy otter" with the Docker stack running.
