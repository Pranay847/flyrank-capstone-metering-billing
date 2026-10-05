#!/usr/bin/env bash
# Runs the five acceptance probes (brief, Section 12) against a running server.
#
#   BASE_URL=http://localhost:8000 ADMIN_TOKEN=... WEBHOOK_SECRET=whsec_... ./scripts/probes.sh
#
# Re-runnable: every probe creates a fresh tenant and fresh keys/event ids.
# Probes 3-4 sign events with WEBHOOK_SECRET exactly the way Stripe does
# (HMAC-SHA256 over "timestamp.payload"); for a live Stripe run use
# `stripe listen` + `stripe trigger` instead (see README).
set -euo pipefail

BASE=${BASE_URL:-http://localhost:8000}
ADMIN=${ADMIN_TOKEN:?set ADMIN_TOKEN to the same value the server uses}
SECRET=${WEBHOOK_SECRET:?set WEBHOOK_SECRET to the server STRIPE_WEBHOOK_SECRET}
RUN=$(date +%s)

hr()   { printf '\n=============== %s ===============\n' "$1"; }
pp()   { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))'; }
field(){ python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

new_tenant() {
  curl -s -X POST "$BASE/tenants" -H "X-Admin-Token: $ADMIN" \
       -H 'Content-Type: application/json' -d "{\"name\":\"$1\"}"
}

call() {  # call METHOD PATH KEY [IDEM] [BODY]  -> prints status, notable headers, body
  local method=$1 path=$2 key=$3 idem=${4:-} body=${5:-}
  local args=(-s -D /tmp/probe_h -o /tmp/probe_b -w '%{http_code}' -X "$method" "$BASE$path"
              -H "X-API-Key: $key" -H 'Content-Type: application/json')
  [ -n "$idem" ] && args+=(-H "Idempotency-Key: $idem")
  [ -n "$body" ] && args+=(-d "$body")
  local code; code=$(curl "${args[@]}")
  echo "HTTP $code"
  grep -iE '^(idempotent-replayed|retry-after):' /tmp/probe_h | tr -d '\r' || true
  pp < /tmp/probe_b
}

sign_and_post() {  # sign_and_post SECRET PAYLOAD
  local ts sig; ts=$(date +%s)
  sig=$(printf '%s' "$ts.$2" | openssl dgst -sha256 -hmac "$1" | sed 's/^.* //')
  curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/webhooks/stripe" \
       -H "Stripe-Signature: t=$ts,v1=$sig" -H 'Content-Type: application/json' -d "$2"
}

checkout_evt() {  # checkout_evt EVENT_ID TENANT_ID
  printf '{"id":"%s","object":"event","type":"checkout.session.completed","created":%s,"data":{"object":{"id":"cs_test_%s","object":"checkout.session","mode":"subscription","payment_status":"paid","client_reference_id":"%s","customer":"cus_%s","subscription":"sub_%s","metadata":{"tenant_id":"%s"}}}}' \
    "$1" "$(date +%s)" "$RUN" "$2" "$RUN" "$1" "$2"
}

# ---------------------------------------------------------------------------
hr "PROBE 1 - same billable request twice, one Idempotency-Key"
T=$(new_tenant "probe1-$RUN"); K=$(echo "$T" | field 'd["api_key"]')
BODY='{"prompt":"hello","usage":{"input_tokens":500,"output_tokens":100}}'
echo "--- first request";  call POST /generate "$K" "p1-$RUN" "$BODY"
echo "--- retry, same key"; call POST /generate "$K" "p1-$RUN" "$BODY"
echo "--- usage after both"; curl -s "$BASE/usage" -H "X-API-Key: $K" | field '"api_calls used = %s" % d["api_calls"]["used"]'

hr "PROBE 2 - drive a tenant to its exact quota"
T=$(new_tenant "probe2-$RUN"); K=$(echo "$T" | field 'd["api_key"]')
echo "--- one request for exactly the Free token quota (100,000 tokens)"
call POST /generate "$K" "p2-fill-$RUN" '{"prompt":"x","usage":{"input_tokens":100000,"output_tokens":0}}'
echo "--- one more token after the boundary"
call POST /generate "$K" "p2-over-$RUN" '{"prompt":"x","usage":{"input_tokens":1,"output_tokens":0}}'
echo "--- background job: the fill crossed 80% and 100%; waiting for the worker"
sleep 4
curl -s "$BASE/notifications" -H "X-API-Key: $K" | pp

hr "PROBE 3 - checkout webhook flips Free -> Pro; /usage shows new limits"
T=$(new_tenant "probe3-$RUN"); K=$(echo "$T" | field 'd["api_key"]'); TID=$(echo "$T" | field 'd["tenant_id"]')
echo "--- before"; curl -s "$BASE/usage" -H "X-API-Key: $K" | field '"plan=%s api_calls_limit=%s ai_tokens_limit=%s" % (d["plan"]["code"], d["api_calls"]["limit"], d["ai_tokens"]["limit"])'
echo "--- signed checkout.session.completed"; sign_and_post "$SECRET" "$(checkout_evt "evt_p3_$RUN" "$TID")"
echo "--- after";  curl -s "$BASE/usage" -H "X-API-Key: $K" | field '"plan=%s status=%s api_calls_limit=%s ai_tokens_limit=%s" % (d["plan"]["code"], d["subscription_status"], d["api_calls"]["limit"], d["ai_tokens"]["limit"])'

hr "PROBE 4 - forged webhook -> 400; real event replayed twice -> processed once"
T=$(new_tenant "probe4-$RUN"); K=$(echo "$T" | field 'd["api_key"]'); TID=$(echo "$T" | field 'd["tenant_id"]')
EVT=$(checkout_evt "evt_p4_$RUN" "$TID")
echo "--- forged (signed with the wrong secret)"; sign_and_post "whsec_attacker" "$EVT"
curl -s "$BASE/usage" -H "X-API-Key: $K" | field '"plan after forgery = %s" % d["plan"]["code"]'
echo "--- real event, delivery 1"; sign_and_post "$SECRET" "$EVT"
echo "--- real event, delivery 2 (replay)"; sign_and_post "$SECRET" "$EVT"

hr "PROBE 5 - pinned pricing rules: cached input + reasoning tokens"
T=$(new_tenant "probe5-$RUN"); K=$(echo "$T" | field 'd["api_key"]')
echo "input 10,000 (4,000 cached) + output 2,000 + reasoning 3,000"
echo "expected: fresh 6,000x300,000 + cached 4,000x75,000 + (2,000+3,000)x2,500,000"
echo "        = 14,600,000,000 pico-USD tokens + 1,000,000,000 call = 15,600,000,000 (\$0.015600)"
echo "--- POST /generate: metered + cost"
curl -s -X POST "$BASE/generate" -H "X-API-Key: $K" -H "Idempotency-Key: p5-$RUN" \
     -H 'Content-Type: application/json' \
     -d '{"prompt":"price check","usage":{"input_tokens":10000,"cached_input_tokens":4000,"output_tokens":2000,"reasoning_tokens":3000}}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"metered": d["metered"], "cost": d["cost"]}, indent=2))'
echo "--- GET /usage: cost block must match"
curl -s "$BASE/usage" -H "X-API-Key: $K" \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["cost"], indent=2))'
