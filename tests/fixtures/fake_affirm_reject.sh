#!/usr/bin/env bash
set -euo pipefail

response_path="${ACCRUVIA_LLM_RESPONSE_PATH:?}"

{
  echo "I would reject this candidate."
  echo
  echo "It is not ready to promote."
} > "${response_path}"
