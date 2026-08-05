#!/usr/bin/env bash
# Minimal end-to-end smoke test against a running bridge (mock or real).
# Usage:
#   BRIDGE_URL=http://localhost:8080 BRIDGE_TOKEN=xxx ./scripts/smoke_test.sh
set -euo pipefail

: "${BRIDGE_URL:=http://localhost:8080}"
: "${BRIDGE_TOKEN:?set BRIDGE_TOKEN}"

auth=(-H "Authorization: Bearer ${BRIDGE_TOKEN}")

echo "== health =="
curl -sS "${BRIDGE_URL}/health" | jq . || curl -sS "${BRIDGE_URL}/health"; echo

echo "== state =="
curl -sS "${auth[@]}" "${BRIDGE_URL}/state" | jq . || true; echo

echo "== actions =="
curl -sS "${auth[@]}" "${BRIDGE_URL}/actions" | jq '.actions[].name' || true; echo

echo "== wave =="
curl -sS "${auth[@]}" -H "Content-Type: application/json" \
  -d '{"action":"wave"}' "${BRIDGE_URL}/command" | jq . || true; echo

echo "== dangerous without confirm (expect 422) =="
curl -sS -o /dev/null -w "%{http_code}\n" "${auth[@]}" \
  -H "Content-Type: application/json" \
  -d '{"action":"zero_torque"}' "${BRIDGE_URL}/command"; echo

echo "== bad token (expect 401) =="
curl -sS -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer nope" \
  -H "Content-Type: application/json" \
  -d '{"action":"wave"}' "${BRIDGE_URL}/command"
