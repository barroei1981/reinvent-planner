"""Optional LLM-based session re-scoring via Amazon Bedrock.

Falls back to keyword scoring silently when:
  - llm.enabled is false in config.yaml
  - boto3 is not installed
  - AWS credentials are missing or lack bedrock:InvokeModel
  - Any Bedrock API call fails

Enable in config.yaml:
  llm:
    enabled: true
    model_id: "anthropic.claude-3-5-haiku-20241022-v1:0"
    region: "us-east-1"
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

from agent.catalog import Session
from agent.scorer import Scorer

logger = logging.getLogger(__name__)

_BATCH_SIZE = 20  # sessions per Bedrock call


def _user_context(preferences: dict) -> str:
    domains = [d["name"] for d in preferences.get("domains", [])]
    aoi = preferences.get("areas_of_interest", [])
    levels = preferences.get("levels", {}).get("preferred", [])
    types = preferences.get("session_types", [])
    return (
        f"Topics: {', '.join(domains)}. "
        f"Areas of interest: {', '.join(aoi)}. "
        f"Preferred levels: {', '.join(levels)}. "
        f"Preferred session types: {', '.join(types)}."
    )


def _build_prompt(sessions: list[Session], context: str) -> str:
    lines = "\n".join(
        f'{i + 1}. id="{s.event_id}" level="{s.learning_level or ""}" '
        f'title="{s.title}" desc="{s.description[:180]}"'
        for i, s in enumerate(sessions)
    )
    return (
        "You are scoring AWS re:Invent 2026 conference sessions for a specific attendee.\n\n"
        f"Attendee preferences:\n{context}\n\n"
        f"Sessions to score (0.0–1.0, where 1.0 = perfect match for this attendee):\n{lines}\n\n"
        "Respond with ONLY a JSON array, one object per session in the same order:\n"
        '[{"id": "<event_id>", "score": <float 0.0-1.0>}, ...]\n\n'
        f"Return exactly {len(sessions)} objects. No explanation, just the JSON array."
    )


class LLMScorer:
    """Bedrock-powered scorer that wraps and falls back to keyword Scorer."""

    def __init__(self, llm_config: dict, preferences: dict) -> None:
        self._fallback = Scorer(preferences)
        self._preferences = preferences
        self._model_id: str = llm_config.get("model_id", "anthropic.claude-3-5-haiku-20241022-v1:0")
        self._region: str = llm_config.get("region", "us-east-1")
        self._context = _user_context(preferences)
        self._client = None  # lazy — avoid boto3 import cost when not used

    def _get_client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def _invoke_batch(self, sessions: list[Session]) -> dict[str, float]:
        """Call Bedrock for one batch. Returns {event_id: score}. Raises on failure."""
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "temperature": 0.0,
            "messages": [{"role": "user", "content": _build_prompt(sessions, self._context)}],
        })
        resp = self._get_client().invoke_model(modelId=self._model_id, body=body)
        text = json.loads(resp["body"].read())["content"][0]["text"].strip()

        start, end = text.find("["), text.rfind("]") + 1
        if start == -1 or end == 0:
            raise ValueError(f"no JSON array in response: {text[:200]}")
        items = json.loads(text[start:end])
        return {item["id"]: float(item["score"]) for item in items}

    def _llm_score(self, sessions: list[Session]) -> list[Session] | None:
        """Score sessions with Bedrock. Returns None to signal fallback."""
        score_map: dict[str, float] = {}
        try:
            for i in range(0, len(sessions), _BATCH_SIZE):
                score_map.update(self._invoke_batch(sessions[i: i + _BATCH_SIZE]))
        except Exception as exc:
            logger.debug("Bedrock scoring failed (%s) — falling back to keyword scorer", exc)
            return None

        scored = [replace(s, score=round(score_map.get(s.event_id, 0.5), 4)) for s in sessions]
        return sorted(scored, key=lambda s: s.score, reverse=True)

    # ── Public API — mirrors Scorer ───────────────────────────────────────────

    def score_all(self, sessions: list[Session]) -> list[Session]:
        result = self._llm_score(sessions)
        return result if result is not None else self._fallback.score_all(sessions)

    def score_reinvent_sessions(self, sessions: list) -> list[Session]:
        # Convert ReinventSession → Session using keyword scorer's logic
        converted = self._fallback.score_reinvent_sessions(sessions)
        if not converted:
            return converted

        wishlisted = [s for s in converted if s.score == 1.0]
        others = [s for s in converted if s.score != 1.0]

        if not others:
            return wishlisted

        llm_scored = self._llm_score(others)
        if llm_scored is None:
            return converted  # keyword-scored result is fine

        # Keep wishlisted at 1.0; scale LLM scores to [0, 0.99] so they never tie
        result = wishlisted + [replace(s, score=round(s.score * 0.99, 4)) for s in llm_scored]
        return sorted(result, key=lambda s: s.score, reverse=True)
