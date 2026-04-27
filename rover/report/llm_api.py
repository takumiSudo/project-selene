"""
Anthropic API wrapper for the reporting phase.

Enforces structured output via tool_choice so the model cannot return freeform
prose.  All prompts are loaded from prompts.txt by name, so the templates can
be edited without touching code.

Public surface:
    load_prompt(name)                  → prompt template string
    LLMClient(api_key, model=...)
        .is_available
        .call_with_tool(schema, prompt)  → dict | None
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPT_BLOCK_START = "=== "
_PROMPT_BLOCK_END = "=== END ==="


def load_prompt(name: str, prompts_path: Path | None = None) -> str:
    """Load a named prompt block from prompts.txt.

    Block format:
        === NAME ===
        <prompt body>
        === END ===
    """
    if prompts_path is None:
        prompts_path = Path(__file__).parent / "prompts.txt"
    text = prompts_path.read_text()

    marker_start = f"{_PROMPT_BLOCK_START}{name} ==="
    start = text.find(marker_start)
    if start == -1:
        raise KeyError(f"Prompt {name!r} not found in {prompts_path}")
    start += len(marker_start)

    end = text.find(_PROMPT_BLOCK_END, start)
    if end == -1:
        raise KeyError(f"Prompt {name!r} has no closing '=== END ===' marker")

    return text[start:end].strip()


class LLMClient:
    """Thin wrapper around the Anthropic SDK with structured tool-use.

    Falls back gracefully when the API key is missing or the SDK is not
    installed — `is_available` becomes False and `call_with_tool` returns None.
    The reporter's section builders then use deterministic templates instead.
    """

    def __init__(self, api_key: str | None, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key
        self.model = model
        self._client = None

        if not api_key:
            logger.info("LLMClient: no API key — running in deterministic-fallback mode")
            return

        try:
            import anthropic
        except ImportError:
            logger.warning("LLMClient: anthropic SDK not installed — falling back to deterministic mode")
            return

        try:
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        except Exception as exc:
            logger.warning("LLMClient: SDK construction failed (%s) — falling back", exc)

    @property
    def is_available(self) -> bool:
        return self._client is not None

    async def call_with_tool(
        self,
        tool_schema: dict,
        prompt: str,
        max_tokens: int = 4096,
    ) -> dict | None:
        """Call the API forcing the model to respond via the named tool.

        Returns the tool's `input` dict, or None if anything fails.  The caller
        is responsible for falling back to deterministic content when None is
        returned.
        """
        if not self.is_available:
            return None

        tool_name = tool_schema["name"]
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                tools=[tool_schema],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            logger.error("LLM call %s failed: %s", tool_name, exc)
            return None

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return dict(block.input)

        logger.warning("LLM response for %s did not contain expected tool_use block", tool_name)
        return None


# ── Tool schemas (the only structured outputs the reporter requests) ─────────

CRISIS_NARRATIVE_TOOL: dict = {
    "name": "write_crisis_narrative",
    "description": (
        "Return a concise, citation-grounded narrative of how Aquifer became "
        "the colony's transitive single point of failure over 18 months."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "One-sentence summary of the crisis",
            },
            "paragraphs": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 3,
                "description": (
                    "2–3 paragraphs total under 250 words.  Each claim must "
                    "include at least one citation in the form "
                    "[pod:logs:TS] / [pod:comms:TS] / [edge:S->T:R] / "
                    "[directive:ID] / [pod:metadata:POD:FIELD]."
                ),
            },
        },
        "required": ["headline", "paragraphs"],
    },
}

RECOMMENDATIONS_TOOL: dict = {
    "name": "recommend_actions",
    "description": (
        "Return 5–7 prioritized infrastructure remediation actions for the "
        "Selene colony, each with rationale and verifiable citations."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "minItems": 5,
                "maxItems": 7,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Verb-led action title (e.g. 'Restore Vault water reserve')",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Why this action — 1–3 sentences with embedded citations",
                        },
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Citation strings, each in [pod:logs:TS] / [edge:S->T:R] / [directive:ID] / [pod:metadata:POD:FIELD] form",
                        },
                        "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                        "effort":   {"type": "string", "enum": ["small", "medium", "large"]},
                    },
                    "required": ["title", "rationale", "evidence", "priority", "effort"],
                },
            },
        },
        "required": ["actions"],
    },
}
