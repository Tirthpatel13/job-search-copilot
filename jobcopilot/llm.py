"""Claude access behind a tiny interface, so the rest of the code (and the tests)
only depend on `StructuredLLM.generate`.

Every call asks for JSON that matches a Pydantic schema (structured outputs),
so responses are validated before they reach the database.
"""

from __future__ import annotations

import logging
from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel

from .config import Settings

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)

GROUNDING_RULES = """\
You are helping one job seeker. Ground rules that override everything else:
1. Use ONLY facts stated in the candidate profile provided (resume, target criteria,
   experience answers). Never invent or embellish skills, tools, employers, job titles,
   dates, degrees, certifications, metrics, or achievements.
2. You may reorder, rephrase, condense, and emphasise real experience.
3. If the job asks for something the profile does not support, say so explicitly as a gap
   or a review item instead of filling it in.
4. Never claim the candidate has applied or will apply; a human submits every application.
"""


class LLMError(RuntimeError):
    """Claude could not produce a usable answer (refusal, truncation, API error)."""


class StructuredLLM(Protocol):
    """Anything that can turn a prompt into a validated Pydantic object."""

    def generate(self, *, system: str, profile: str, prompt: str, schema: type[M]) -> M:
        """Return an instance of `schema` answering `prompt` given the candidate `profile`."""
        ...


class ClaudeLLM:
    """`StructuredLLM` backed by the Anthropic Messages API."""

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        if not settings.anthropic_api_key and client is None:
            raise LLMError("ANTHROPIC_API_KEY is not set; add it to your .env file")
        self.settings = settings
        self.client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def generate(self, *, system: str, profile: str, prompt: str, schema: type[M]) -> M:
        """Call Claude with the profile as a cached system block and parse the result.

        The profile is identical across every scoring call, so it is marked for prompt
        caching; only the job-specific prompt changes between requests.
        """
        system_blocks = [
            {"type": "text", "text": f"{GROUNDING_RULES}\n{system}"},
            {
                "type": "text",
                "text": f"<candidate_profile>\n{profile}\n</candidate_profile>",
                "cache_control": {"type": "ephemeral"},
            },
        ]
        extra: dict = {}
        if self.settings.claude_fallbacks:
            # On a safety-classifier refusal the API retries on a fallback model in-call.
            extra = {"betas": [self.FALLBACK_BETA], "fallbacks": "default"}
        try:
            response = self.client.beta.messages.parse(
                model=self.settings.claude_model,
                max_tokens=16000,
                system=system_blocks,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.settings.claude_effort},
                output_format=schema,
                **extra,
            )
        except anthropic.RateLimitError as exc:
            raise LLMError("Claude rate limit reached; try again shortly") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError("Invalid ANTHROPIC_API_KEY") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Could not reach the Claude API") from exc

        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude's answer was cut off (max_tokens)")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("Claude returned no structured output")
        return parsed
