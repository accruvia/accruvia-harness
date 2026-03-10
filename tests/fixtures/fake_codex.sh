#!/usr/bin/env bash
set -euo pipefail

prompt_path="${ACCRUVIA_LLM_PROMPT_PATH:?}"
response_path="${ACCRUVIA_LLM_RESPONSE_PATH:?}"
model="${ACCRUVIA_LLM_MODEL:-unknown}"

{
  echo "# Fake Codex Response"
  echo
  echo "model=${model}"
  echo "prompt_path=${prompt_path}"
  echo "executor=codex"
} > "${response_path}"
