#!/usr/bin/env bash
set -euo pipefail

response_path="${ACCRUVIA_LLM_RESPONSE_PATH:?}"

{
  echo "# Fake Accruvia Client Response"
  echo
  echo "executor=accruvia_client"
  echo "ci=${GITHUB_ACTIONS:-false}"
} > "${response_path}"
