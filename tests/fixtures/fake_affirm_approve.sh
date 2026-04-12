#!/usr/bin/env bash
set -euo pipefail

response_path="${ACCRUVIA_LLM_RESPONSE_PATH:?}"

cat > "${response_path}" <<'JSON'
{"approved": true, "rationale": "The deterministic gates passed and the candidate is acceptable for promotion.", "concerns": [], "summary": "Approved for promotion"}
JSON
