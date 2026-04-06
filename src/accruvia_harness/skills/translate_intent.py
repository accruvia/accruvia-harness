"""The /translate-intent skill — natural language to technical task.

The entry point for non-developers. Takes a plain-language feature request
and produces a structured technical task specification that the rest of
the skills pipeline can execute.

A developer would write:
    "Edit src/auth/cache.py to add a TTL cache keyed by token signature..."

A non-developer writes:
    "I want login to be faster by caching user sessions."

This skill bridges the gap. It produces:
    - A concrete technical objective (for /scope and /implement)
    - Suggested file scope (allowed_paths)
    - Acceptance criteria in plain English (for /verify-acceptance)
    - Validation profile recommendation
    - Estimated complexity
    - Risks in business terms

The skill uses the LLM to reason about the codebase structure (via
repo_context and related_files) and translate intent into a plan.
"""
from __future__ import annotations

from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class TranslateIntentSkill:
    """Translate natural language intent into a structured technical task."""

    name = "translate_intent"
    output_schema: dict[str, Any] = {
        "required": [
            "technical_objective",
            "acceptance_criteria",
            "suggested_files",
            "validation_profile",
            "estimated_complexity",
            "risks_plain_language",
            "summary_for_requester",
            "why_chain",
            "mermaid_diagram",
        ],
        "types": {
            "technical_objective": "str",
            "acceptance_criteria": "list",
            "suggested_files": "list",
            "suggested_forbidden_files": "list",
            "validation_profile": "str",
            "estimated_complexity": "str",
            "risks_plain_language": "list",
            "summary_for_requester": "str",
            "why_chain": "list",
            "mermaid_diagram": "str",
        },
        "allowed_values": {
            "estimated_complexity": ["trivial", "small", "medium", "large", "too_large"],
            "validation_profile": ["python", "javascript", "terraform", "generic"],
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        intent = str(inputs.get("intent") or "").strip()
        repo_context = str(inputs.get("repo_context") or "").strip()
        project_description = str(inputs.get("project_description") or "").strip()
        codebase_search_results = inputs.get("codebase_search_results") or {}
        related_file_contents = inputs.get("related_file_contents") or {}

        search_block = ""
        if codebase_search_results:
            parts = ["Codebase search results:"]
            for query, lines in codebase_search_results.items():
                joined = "\n".join(lines)[:500]
                parts.append(f"  {query}:\n{joined}")
            search_block = "\n".join(parts)

        related_block = ""
        if related_file_contents:
            parts = ["Related files:"]
            for path, content in related_file_contents.items():
                parts.append(f"--- {path} ---\n{content[:3000]}")
            related_block = "\n".join(parts)

        return "\n\n".join(
            filter(
                None,
                [
                    "You are translating a non-technical feature request into a "
                    "concrete technical task specification. The requester is NOT a "
                    "developer — they describe what they want in business/product "
                    "terms. Your job is to figure out the technical implementation "
                    "details so the coding pipeline can execute.\n\n"
                    "CRITICAL: Before writing the specification, apply the 6 Whys "
                    "technique to drill from the surface request to the root need. "
                    "Ask yourself 'Why does the requester want this?' six times, each "
                    "answer feeding the next question. Include your 6 Whys reasoning "
                    "chain in a new JSON key 'why_chain' (list of 6 strings, each "
                    "a why→because pair). The final why should reveal the root "
                    "technical action. Build your technical_objective from that root, "
                    "not from the surface request.",
                    f"Feature request (verbatim from requester):\n{intent}",
                    f"Project description: {project_description or '(not provided)'}",
                    "Repository structure:\n" + (repo_context or "(not provided)"),
                    search_block,
                    related_block,
                    "Return strict JSON with keys:\n"
                    "  technical_objective (string, 2-5 sentences describing EXACTLY "
                    "what code changes are needed, naming specific files, functions, "
                    "and patterns — as if briefing a senior engineer)\n"
                    "  acceptance_criteria (list of strings, each a plain-English "
                    "criterion the requester can verify WITHOUT reading code — e.g. "
                    "'When I click Save, my preferences persist across browser sessions')\n"
                    "  suggested_files (list of file paths that will need to be "
                    "created or modified)\n"
                    "  suggested_forbidden_files (list of file paths that MUST NOT "
                    "be changed — infrastructure, configs, unrelated modules)\n"
                    "  validation_profile (one of: python, javascript, terraform, generic)\n"
                    "  estimated_complexity (one of: trivial, small, medium, large, too_large)\n"
                    "  risks_plain_language (list of strings describing what could "
                    "go wrong, in terms the requester understands — no jargon)\n"
                    "  summary_for_requester (string, 2-3 sentences explaining what "
                    "you plan to build and how, in non-technical terms)",
                    "  why_chain (list of 6 strings, each a 'Why? → Because...' pair "
                    "from the 6 Whys analysis. First why is about the surface request, "
                    "last why reveals the root technical need)\n"
                    "  mermaid_diagram (string, a Mermaid stateDiagram-v2 showing the "
                    "system's understanding: the user's intent at the top, the "
                    "acceptance criteria as states, the technical approach as transitions, "
                    "and the expected outcome. This is rendered in the UI so the user "
                    "can see 'this is what I asked for, this is what will be built.')\n",
                    "GUIDELINES:\n"
                    "  - If the request is vague, make reasonable assumptions and "
                    "state them in the summary_for_requester.\n"
                    "  - If the request is too large for a single task, set "
                    "estimated_complexity='too_large' and explain in the summary "
                    "how you'd split it.\n"
                    "  - Acceptance criteria should be testable by a non-developer. "
                    "Avoid 'unit test passes' — instead 'the feature works when...'.\n"
                    "  - suggested_files should be conservative. Fewer files = safer.\n"
                    "  - risks_plain_language should focus on user-visible risks, "
                    "not implementation risks.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        parsed.setdefault("suggested_forbidden_files", [])
        parsed.setdefault("risks_plain_language", [])
        parsed.setdefault("why_chain", [])
        parsed.setdefault("mermaid_diagram", "")
        for list_key in ("acceptance_criteria", "suggested_files",
                         "suggested_forbidden_files", "risks_plain_language"):
            if isinstance(parsed.get(list_key), list):
                parsed[list_key] = [str(item) for item in parsed[list_key] if item]
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        if not parsed.get("technical_objective", "").strip():
            return False, ["technical_objective must be non-empty"]
        if not parsed.get("acceptance_criteria"):
            return False, ["acceptance_criteria must have at least one criterion"]
        if not parsed.get("suggested_files"):
            return False, ["suggested_files must have at least one file"]
        if not parsed.get("summary_for_requester", "").strip():
            return False, ["summary_for_requester must be non-empty"]
        return True, []

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None
