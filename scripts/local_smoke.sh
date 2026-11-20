#!/usr/bin/env bash
# Smoke test for the running local stack (docker compose). Covers REST routes, the Gradio GPU endpoint and the
# frontend. Run from the repo root in Git Bash:
#   bash scripts/local_smoke.sh .env
# Reads STORYWEAVER_API_KEY and AUTH_SECRET from the given .env without printing them. Spends one tiny Groq call and one Whisper call;
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
  id=$(curl -s -m 30 -X POST "$API/gradio_api/call/$name" -H 'Content-Type: application/json' "$@" -d "${GRADIO_DATA:-{\"data\":[]\}}" \
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
# The worker's picture route (V3): the Gradio endpoint `illustrate`, and this server calling its own
# gpu_check over HTTP when IMAGE_GRADIO_URL is set (404 means pictures are drawn in-process instead).
res=$(GRADIO_DATA='{"data":["{}"]}' gradio_call illustrate)
check "illustrate without key -> error"  error "${res%%|*}"
res=$(GRADIO_DATA='{"data":["{}"]}' gradio_call illustrate -H "X-API-Key: $KEY")
check "illustrate bad job -> invalid"    invalid "$(echo "${res#*|}" | python -I -c 'import json,sys;print(json.load(sys.stdin)[0].get("outcome"))' 2>/dev/null)"
route=$(curl -s -m 150 -w '|%{http_code}' -H "X-API-Key: $KEY" $API/v1/gpu-route-check)
check "gpu route check (404 = in-process)" ok "$(python -I -c 'import json,sys;b,c=sys.argv[1].rsplit("|",1);print("ok" if c=="404" or json.loads(b).get("ok") else b)' "$route" 2>/dev/null)"

echo "--- Sign-in and owners (V3)"
# The smoke test can't sign in with Google, so it signs its own session cookies with AUTH_SECRET, exactly as the
# callback does (frontend/lib/auth.ts). Each run uses two fresh random parents, so reruns never share a limit.
AUTH=$(grep -E '^AUTH_SECRET=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')
GOOGLE_ID=$(grep -E '^GOOGLE_CLIENT_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')
session_cookie() { python -I -c '
import base64, hashlib, hmac, json, sys, time
key = hmac.new(sys.argv[1].encode(), b"storyweaver session v1", hashlib.sha256).digest()
b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
now = int(time.time())
head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
body = b64(json.dumps({"sub": sys.argv[2], "iat": now, "exp": now + 600}).encode())
print("sw_session=" + head + "." + body + "." + b64(hmac.new(key, (head + "." + body).encode(), hashlib.sha256).digest()))
' "$AUTH" "$(python -I -c 'import secrets;print(secrets.token_urlsafe(32))')"; }
ALICE=$(session_cookie); BOB=$(session_cookie)
problem() { python -I -c 'import json,sys;d=json.load(sys.stdin);print(d.get("code"),d.get(sys.argv[1]),sep="|")' "$1"; }
unsafe_idea() { curl -s -m 30 -X POST $WEB/api/stories -H 'Content-Type: application/json' -H "Cookie: $1" -d '{"prompt":"visit www.example.com with Pip"}'; }
check "home page signed out -> sign-in" 1 "$(curl -s $WEB/ | grep -c 'Sign in with Google')"
if [ -n "$GOOGLE_ID" ]; then
  check "signin -> Google (openid only)" "302|openid" "$(curl -s -o /dev/null -w '%{http_code}|%{redirect_url}' $WEB/api/auth/signin | sed -E 's/^([0-9]+)\|.*[?&]scope=([a-z]+).*/\1|\2/')"
else
  check "signin without Google client -> 503" 503 "$(code $WEB/api/auth/signin)"
fi
check "api/stories signed out -> 401"   "sign_in_required|None" "$(unsafe_idea 'sw_session=forged.cookie.value' | problem window)"
check "api/stories list signed in -> 200" 200 "$(code -H "Cookie: $ALICE" $WEB/api/stories)"
check "api/stories other site -> 403"   403 "$(code -X POST $WEB/api/stories -H "Cookie: $ALICE" -H 'Origin: https://evil.example' -H 'Content-Type: application/json' -d '{"prompt":"an owl"}')"
check "backend forged user token -> 401" "sign_in_required|None" "$(curl -s -m 30 $API/v1/stories -H "X-API-Key: $KEY" -H 'X-User-Token: a.b.c' | problem window)"
# The unsafe idea is a URL: the rules reject it before any model call, so no Groq quota is used.
check "api/stories unsafe idea -> 422"  "unsafe_input|urls" "$(unsafe_idea "$ALICE" | problem category)"
# Per-parent limit (5 an hour): ideas count even when rejected, so the 6th is refused, and only for that parent.
for _ in 1 2 3 4; do unsafe_idea "$ALICE" >/dev/null; done
check "api/stories 6th from parent -> limit" "visitor_limit|hour" "$(unsafe_idea "$ALICE" | problem window)"
check "api/stories other parent unaffected" "unsafe_input|urls" "$(unsafe_idea "$BOB" | problem category)"
check "api/stories bad JSON -> 400"     400 "$(code -X POST $WEB/api/stories -H "Cookie: $ALICE" -H 'Content-Type: application/json' -d '{bad')"
check "api/stories/<bad id> -> 404"     404 "$(code -H "Cookie: $ALICE" $WEB/api/stories/not-a-uuid)"
check "api/stories/<unknown> -> 404"    404 "$(code -H "Cookie: $ALICE" $WEB/api/stories/00000000-0000-4000-8000-000000000000)"
check "DELETE api/stories/<unknown> -> 404" 404 "$(code -X DELETE -H "Cookie: $ALICE" $WEB/api/stories/00000000-0000-4000-8000-000000000000)"
check "DELETE api/stories/<bad id> -> 404" 404 "$(code -X DELETE -H "Cookie: $ALICE" $WEB/api/stories/not-a-uuid)"
check "shared link malformed -> 404"    404 "$(code $WEB/api/shared/not-a-token)"
check "shared link forged -> 404"       404 "$(code $WEB/api/shared/$(printf 'a%.0s' {1..32}).1800000000.$(printf 'b%.0s' {1..32}))"

echo "--- Speech -> picture (V3)"
picture() { curl -s -m 60 -X POST $WEB/api/pictures -H 'Content-Type: application/json' -H "Cookie: $1" -d "{\"prompt\":\"$2\"}"; }
check "api/pictures signed out -> 401"  "sign_in_required|None" "$(picture 'sw_session=forged.cookie.value' 'draw a cat' | problem window)"
# Nothing left once "draw me please" is taken out: refused before any model call or limit.
check "api/pictures nothing to draw -> 422" "nothing_to_draw|None" "$(picture "$BOB" 'draw me please' | problem window)"
check "api/pictures unsafe -> 422"      "unsafe_input|urls" "$(picture "$BOB" 'draw www.example.com' | problem category)"
# A real picture: Prompt Guard + safeguard (2 small Groq calls), then the worker draws it on the local GPU.
# The safety model allows a couple of requests a minute: if it is busy (429 + Retry-After), wait and ask once more.
draw_dragon() { curl -s -m 60 -D "$TMP_HEADERS" -X POST $WEB/api/pictures -H 'Content-Type: application/json' -H "Cookie: $BOB" -d '{"prompt":"Can you draw me a purple dragon, please?"}'; }
TMP_HEADERS=$(mktemp)
drawn=$(draw_dragon)
if grep -qi '^HTTP/[0-9.]* 429' "$TMP_HEADERS"; then
  wait_s=$(grep -i '^retry-after:' "$TMP_HEADERS" | tr -dc '0-9'); sleep $(( ${wait_s:-30} + 1 )); drawn=$(draw_dragon)
fi
rm -f "$TMP_HEADERS"
PIC=$(echo "$drawn" | python -I -c 'import json,sys;print(json.load(sys.stdin).get("story_id",""))' 2>/dev/null)
pic_state=""
for _ in $(seq 1 60); do
  pic_state=$(curl -s -m 30 -H "Cookie: $BOB" "$WEB/api/stories/$PIC" | python -I -c '
import json, sys
d = json.load(sys.stdin)
s = d.get("scenes") or [{}]
print(d.get("kind"), d.get("title"), len(s), s[0].get("image_status"), bool(s[0].get("image_url")), sep="|")' 2>/dev/null)
  case "$pic_state" in *"|pending|"*|"") sleep 3 ;; *) break ;; esac
done
check "api/pictures draws one page"     "picture|A purple dragon|1|ready|True" "$pic_state"
check "picture is the parent's only"    404 "$(code -H "Cookie: $ALICE" $WEB/api/stories/$PIC)"
check "picture listed as a picture"     True "$(curl -s -m 30 -H "Cookie: $BOB" $WEB/api/stories | python -I -c 'import json,sys;print(any(s["id"]==sys.argv[1] and s["kind"]=="picture" for s in json.load(sys.stdin)["stories"]))' "$PIC" 2>/dev/null)"
check "DELETE picture -> 204"           204 "$(code -X DELETE -H "Cookie: $BOB" $WEB/api/stories/$PIC)"

echo "--- Frontend"
check "home page 200"                   200 "$(code $WEB/)"
check "status page 200"                 200 "$(code $WEB/status)"
check "story page 200"                  200 "$(code $WEB/story/00000000-0000-4000-8000-000000000000)"
check "shared story page 200"           200 "$(code $WEB/s/not-a-token)"
check "privacy page 200"                200 "$(code $WEB/privacy)"
# Voice proxy: the Vercel route holds the API key and checks the recording before forwarding it.
check "web transcribe signed out -> 401" 401 "$(code -X POST $WEB/api/speech/transcribe -H 'Content-Type: audio/wav' --data-binary @$FIXTURE)"
heard_web=$(curl -s -m 120 -X POST $WEB/api/speech/transcribe -H "Cookie: $ALICE" -H 'Content-Type: audio/wav' --data-binary @$FIXTURE)
check "web transcribe speech -> text"   True "$(echo "$heard_web" | python -I -c 'import json,sys;print("dragon" in json.load(sys.stdin).get("text","").lower())' 2>/dev/null)"
check "web transcribe JSON -> 415"      415 "$(code -X POST $WEB/api/speech/transcribe -H "Cookie: $ALICE" -H 'Content-Type: application/json' -d '{}')"
check "web transcribe 5 MB -> 413"      413 "$(head -c 5000000 /dev/zero | curl -s -o /dev/null -w '%{http_code}' -m 60 -X POST $WEB/api/speech/transcribe -H "Cookie: $ALICE" -H 'Content-Type: audio/webm' --data-binary @-)"
status=$(curl -s -m 180 $WEB/api/status)
check "status: backend ok"  True "$(echo "$status" | python -I -c 'import json,sys;print(json.load(sys.stdin)["backend"]["ok"])')"
check "status: gpu ok"      True "$(echo "$status" | python -I -c 'import json,sys;print(json.load(sys.stdin)["gpu"]["ok"])')"
check "status: groq ok"     True "$(echo "$status" | python -I -c 'import json,sys;print(json.load(sys.stdin)["groq"]["ok"])')"
# Security: the API key must never reach the browser (HTML or any JS chunk).
leak=0; page=$(curl -s $WEB/); echo "$page" | grep -qF "$KEY" && leak=1
for js in $(echo "$page" | grep -oE '/_next/static/[^"]+\.js' | sort -u); do curl -s "$WEB$js" | grep -qF "$KEY" && leak=1; done
check "API key absent from browser HTML/JS" 0 "$leak"
leak=0; for secret in "$AUTH" "$(grep -E '^USER_TOKEN_SECRET=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')"; do
  page=$(curl -s -H "Cookie: $ALICE" $WEB/); echo "$page" | grep -qF "$secret" && leak=1
  for js in $(echo "$page" | grep -oE '/_next/static/[^"]+\.js' | sort -u); do curl -s "$WEB$js" | grep -qF "$secret" && leak=1; done
done
check "sign-in secrets absent from browser" 0 "$leak"

echo "--- Totals: $pass passed, $fail failed"

[ "$fail" -eq 0 ]
