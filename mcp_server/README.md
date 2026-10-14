# StoryWeaver MCP server

A thin MCP adapter over the StoryWeaver REST API (plan §6.7). It holds no business logic: every tool calls
`/v1`. V1 runs over **stdio** (Claude Desktop, MCP Inspector); V9 adds Streamable HTTP on the Space.

| Kind | Name | Calls |
|---|---|---|
| tool | `create_story(prompt, age_band="4-6", num_scenes=5)` | `POST /v1/stories` |
| tool | `get_story(story_id)` | `GET /v1/stories/{id}` |
| tool | `get_job_status(job_id)` | `GET /v1/jobs/{id}` |
| resource | `story://{story_id}` | `GET /v1/stories/{id}` |
| prompt | `bedtime_story(favourite_animal, theme)` | none |

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `STORYWEAVER_API_URL` | `http://localhost:7860` | The local Docker API; the Space URL once V1 is deployed |
| `STORYWEAVER_API_KEY` | (required) | The same key as the backend's `STORYWEAVER_API_KEY` |

## Commands (from this folder)

```powershell
uv sync                     # install (you run this)
uv run pytest               # tests: real MCP protocol in memory, REST API mocked
uv run mcp dev server.py    # MCP Inspector in the browser (needs Node/npx)
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
