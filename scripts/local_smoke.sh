#!/usr/bin/env bash
# Smoke test for the running local stack (docker compose). Covers REST routes, the Gradio GPU endpoint and the
# frontend. Run from the repo root in Git Bash:
#   bash scripts/local_smoke.sh .env
# Reads STORYWEAVER_API_KEY from the given .env without printing it. Spends one tiny Groq call and one Whisper call;
# text-to-speech uses Kokoro only.
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
# Browsers enforce CORS via Access-Control-Allow-Origin; Gradio's own CORS layer answers 200 without it.
acao=$(curl -s -D - -o /dev/null -X OPTIONS $API/v1/echo -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: POST' | tr -d '\r' | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')
check "CORS gives other origin no ACAO" "" "$acao"
acao=$(curl -s -D - -o /dev/null -X OPTIONS $API/gradio_api/call/gpu_check -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: POST' | tr -d '\r' | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')
check "CORS (Gradio route) no ACAO"     "" "$acao"
# Gradio's own CORS layer reflects any origin when Host is not localhost (as on the Space); simulate that host.
acao=$(curl -s -D - -o /dev/null -X OPTIONS $API/v1/echo -H 'Host: storyweaver.hf.space' -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: POST' | tr -d '\r' | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')
check "CORS (Space host) no ACAO"       "" "$acao"
check "unknown route -> 404"            404 "$(code -H "X-API-Key: $KEY" $API/v1/nope)"
check "groq-check -> 200 (1 tiny call)" 200 "$(code -H "X-API-Key: $KEY" $API/v1/groq-check)"
check "/docs available locally"         200 "$(code $API/docs)"
check "REST gpu-check removed -> 404"   404 "$(code -H "X-API-Key: $KEY" $API/v1/gpu-check)"

echo "--- Speech-to-text (V2)"
STT=$API/v1/speech/transcribe
FIXTURE=backend/tests/fixtures/speech_dragon.wav
check "transcribe without key -> 401"   401 "$(code -X POST $STT -H 'Content-Type: audio/wav' --data-binary @$FIXTURE)"
check "transcribe JSON body -> 415"     415 "$(code -X POST $STT -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{}')"
silent=$(python -I -c "import io,sys,wave;b=io.BytesIO();w=wave.open(b,'wb');w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(bytes(96000));w.close();sys.stdout.buffer.write(b.getvalue())" \
  | curl -s -m 60 -X POST $STT -H "X-API-Key: $KEY" -H 'Content-Type: audio/wav' --data-binary @-)
check "transcribe silence -> audio_silent" audio_silent "$(echo "$silent" | python -I -c 'import json,sys;print(json.load(sys.stdin).get("code"))' 2>/dev/null)"
heard=$(curl -s -m 120 -X POST $STT -H "X-API-Key: $KEY" -H 'Content-Type: audio/wav' --data-binary @$FIXTURE)
check "transcribe speech (1 Whisper call)" True "$(echo "$heard" | python -I -c 'import json,sys;print("dragon" in json.load(sys.stdin).get("text","").lower())' 2>/dev/null)"

echo "--- Text-to-speech (V2; Kokoro only, so the tiny Orpheus quota is never spent)"
NAR=$API/v1/speech/narrate
check "narrate without key -> 401"      401 "$(code -X POST $NAR -H 'Content-Type: application/json' -d '{"text":"Hi."}')"
check "narrate unknown voice -> 422"    422 "$(code -X POST $NAR -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"text":"Hi.","voice":"nobody"}')"
check "voices without key -> 401"       401 "$(code $API/v1/speech/voices)"
check "voices lists both engines"       True "$(curl -s -m 30 $API/v1/speech/voices -H "X-API-Key: $KEY" | python -I -c 'import json,sys;print(sorted(json.load(sys.stdin)["engines"])==["kokoro","orpheus"])' 2>/dev/null)"
# The first Kokoro call downloads its model (~350 MB) into the mounted cache, so allow a few minutes.
nar=$(curl -s -m 600 -o /dev/null -w '%{http_code}|%{content_type}' -D - -X POST $NAR -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"text":"Goodnight, little dragon.","voice":"af_heart"}' | tr -d '\r' | awk -F': ' 'tolower($1)=="x-tts-engine"{e=$2} END{print e}')
check "narrate with Kokoro -> mp3"      kokoro "$nar"

echo "--- Gradio GPU endpoint (ADR-0001)"
# gradio_call <api name> [curl header args...] -> prints "<event>|<data>" of the final SSE event
gradio_call() {
  local name=$1; shift
  local id
  id=$(curl -s -m 30 -X POST "$API/gradio_api/call/$name" -H 'Content-Type: application/json' "$@" -d '{"data":[]}' \
    | python -I -c "import json,sys;print(json.load(sys.stdin).get('event_id',''))" 2>/dev/null)
  [ -n "$id" ] || { echo "no-event-id|"; return; }
  curl -s -m 120 "$API/gradio_api/call/$name/$id" "$@" | tr -d '\r' \
    | awk '/^event:/{e=substr($0,8)} /^data:/{d=substr($0,7)} END{print e "|" d}'
}
res=$(gradio_call gpu_check -H "X-API-Key: $KEY")
check "gpu_check with key completes"    complete "${res%%|*}"
check "gpu_check ran on CUDA"           True "$(echo "${res#*|}" | python -I -c 'import json,sys;print(json.load(sys.stdin)[0].get("ok"))' 2>/dev/null)"
res=$(gradio_call gpu_check)
check "gpu_check without key -> error"  error "${res%%|*}"
res=$(gradio_call gpu_check -H 'X-API-Key: wrong')
check "gpu_check wrong key -> error"    error "${res%%|*}"

echo "--- Frontend"
check "home page 200"                   200 "$(code $WEB/)"
check "status page 200"                 200 "$(code $WEB/status)"
check "story page 200"                  200 "$(code $WEB/story/00000000-0000-4000-8000-000000000000)"
check "privacy page 200"                200 "$(code $WEB/privacy)"
# Story routes. The unsafe idea is a URL: the rules reject it before any model call, so no Groq quota is used.
unsafe=$(curl -s -m 30 -X POST $WEB/api/stories -H 'Content-Type: application/json' -d '{"prompt":"visit www.example.com with Pip"}')
check "api/stories unsafe idea -> 422"  "unsafe_input|urls" "$(echo "$unsafe" | python -I -c 'import json,sys;d=json.load(sys.stdin);print(d.get("code"),d.get("category"),sep="|")')"
check "api/stories bad JSON -> 400"     400 "$(code -X POST $WEB/api/stories -H 'Content-Type: application/json' -d '{bad')"
check "api/stories/<bad id> -> 404"     404 "$(code $WEB/api/stories/not-a-uuid)"
check "api/stories/<unknown> -> 404"    404 "$(code $WEB/api/stories/00000000-0000-4000-8000-000000000000)"
check "DELETE api/stories/<unknown> -> 404" 404 "$(code -X DELETE $WEB/api/stories/00000000-0000-4000-8000-000000000000)"
check "DELETE api/stories/<bad id> -> 404" 404 "$(code -X DELETE $WEB/api/stories/not-a-uuid)"
# Voice proxy: the Vercel route holds the API key and checks the recording before forwarding it.
heard_web=$(curl -s -m 120 -X POST $WEB/api/speech/transcribe -H 'Content-Type: audio/wav' --data-binary @$FIXTURE)
check "web transcribe speech -> text"   True "$(echo "$heard_web" | python -I -c 'import json,sys;print("dragon" in json.load(sys.stdin).get("text","").lower())' 2>/dev/null)"
check "web transcribe JSON -> 415"      415 "$(code -X POST $WEB/api/speech/transcribe -H 'Content-Type: application/json' -d '{}')"
check "web transcribe 5 MB -> 413"      413 "$(head -c 5000000 /dev/zero | curl -s -o /dev/null -w '%{http_code}' -m 60 -X POST $WEB/api/speech/transcribe -H 'Content-Type: audio/webm' --data-binary @-)"
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
