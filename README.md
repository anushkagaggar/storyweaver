# StoryWeaver

Voice-first, illustrated children's audiobook generator (text ↔ image ↔ speech). See [plan.md](plan.md)
for the full design and roadmap.

## Local development

```powershell
copy .env.example .env                       # then fill in the secrets
cd backend; uv sync --extra gpu; uv run pytest -m unit
uv run python app.py                         # http://localhost:7860/docs, /ui
cd ..\frontend; copy .env.example .env.local; npm install; npm run dev   # http://localhost:3000

# Full local stack (GPU API, web, postgres, redis, SeaweedFS S3):
docker compose -f infra/docker-compose.yml --env-file .env up --build
```

## Deployment

- **Backend:** pushing a `v*` tag runs `.github/workflows/deploy-backend.yml`, which uploads `backend/` to the HF Space.
- **Frontend:** Vercel Git integration with root directory `frontend/`.
