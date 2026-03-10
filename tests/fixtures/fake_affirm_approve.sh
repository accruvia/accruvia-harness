#!/usr/bin/env bash
set -euo pipefail

response_path="${ACCRUVIA_LLM_RESPONSE_PATH:?}"

{
  echo "APPROVE"
  echo
  echo "The deterministic gates passed and the candidate is acceptable for promotion."
} > "${response_path}"
