#!/usr/bin/env bash
# Smoke test for the running local stack (docker compose). Run from the repo root in Git Bash:
#   bash scripts/local_smoke.sh .env
# Reads STORYWEAVER_API_KEY from the given .env without printing it. Spends one tiny Groq call.
set -u
ENV_FILE=${1:-.env}
[ -f "$ENV_FILE" ] || { echo "Env file not found: $ENV_FILE" >&2; exit 2; }
KEY=$(grep -E '^STORYWEAVER_API_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')
API=http://localhost:7860; WEB=http://localhost:3000
pass=0; fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1 (expected $2, got $3)"; fail=$((fail+1)); fi; }
code() { curl -s -o /dev/null -w "%{http_code}" -m 120 "$@"; }

echo "--- Backend API"
check "health/live 200"                 200 "$(code $API/health/live)"
check "health/ready 200"                200 "$(code $API/health/ready)"
check "echo without key -> 401"         401 "$(code -X POST $API/v1/echo -H 'Content-Type: application/json' -d '{"message":"hi"}')"
check "echo wrong key -> 401"           401 "$(code -X POST $API/v1/echo -H 'X-API-Key: wrong' -H 'Content-Type: application/json' -d '{"message":"hi"}')"
check "401 is problem+json"             "application/problem+json" "$(curl -s -o /dev/null -w '%{content_type}' -X POST $API/v1/echo -d '{}')"
check "echo valid -> 200"               200 "$(code -X POST $API/v1/echo -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"message":"hi"}')"
check "echo empty message -> 422"       422 "$(code -X POST $API/v1/echo -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"message":""}')"
long=$(python -I -c "print('a'*1001)")
check "echo 1001 chars -> 422"          422 "$(code -X POST $API/v1/echo -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d "{\"message\":\"$long\"}")"
check "echo malformed JSON -> 422"      422 "$(code -X POST $API/v1/echo -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{bad')"
rid=$(curl -s -D - -o /dev/null -H 'X-Request-ID: smoke-123' $API/health/live | tr -d '\r' | awk -F': ' 'tolower($1)=="x-request-id"{print $2}')
check "X-Request-ID propagated"         smoke-123 "$rid"
acao=$(curl -s -D - -o /dev/null -X OPTIONS $API/v1/echo -H 'Origin: http://localhost:3000' -H 'Access-Control-Request-Method: POST' | tr -d '\r' | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')
check "CORS allows localhost:3000"      http://localhost:3000 "$acao"
check "CORS rejects other origin"       400 "$(code -X OPTIONS $API/v1/echo -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: POST')"
check "unknown route -> 404"            404 "$(code -H "X-API-Key: $KEY" $API/v1/nope)"
check "gpu-check -> 200 (GPU in Docker)" 200 "$(code -H "X-API-Key: $KEY" $API/v1/gpu-check)"
check "groq-check -> 200 (1 tiny call)" 200 "$(code -H "X-API-Key: $KEY" $API/v1/groq-check)"
check "Gradio /ui/ -> 200"              200 "$(code $API/ui/)"
check "/docs available locally"         200 "$(code $API/docs)"

echo "--- Frontend"
check "home page 200"                   200 "$(code $WEB/)"
status=$(curl -s -m 180 $WEB/api/status)
check "status: backend ok"  True "$(echo "$status" | python -I -c 'import json,sys;print(json.load(sys.stdin)["backend"]["ok"])')"
check "status: gpu ok"      True "$(echo "$status" | python -I -c 'import json,sys;print(json.load(sys.stdin)["gpu"]["ok"])')"
check "status: groq ok"     True "$(echo "$status" | python -I -c 'import json,sys;print(json.load(sys.stdin)["groq"]["ok"])')"
# Security: the API key must never reach the browser (HTML or any JS chunk).
leak=0; page=$(curl -s $WEB/); echo "$page" | grep -qF "$KEY" && leak=1
for js in $(echo "$page" | grep -oE '/_next/static/[^"]+\.js' | sort -u); do curl -s "$WEB$js" | grep -qF "$KEY" && leak=1; done
check "API key absent from browser HTML/JS" 0 "$leak"

echo "--- Totals: $pass passed, $fail failed"

[ "$fail" -eq 0 ]
