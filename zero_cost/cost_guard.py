from __future__ import annotations

import os


class CostGuard:
    """Hard stop against unapproved or uncapped paid production paths."""

    def __init__(self) -> None:
        self.allow_paid_api = os.getenv("ALLOW_PAID_API", "false").lower() == "true"
        self.max_episode_cost_usd = float(os.getenv("MAX_EPISODE_COST_USD", "0"))

    def require_free(self, provider: str) -> None:
        if provider not in {"ollama", "piper", "ffmpeg", "youtube", "supabase"}:
            raise RuntimeError(f"Provider '{provider}' is not approved by the zero-cost policy")

    def authorize_paid_episode(self, provider: str, estimated_usd: float, scene_count: int) -> None:
        if provider != "ltx":
            raise RuntimeError(f"Paid provider '{provider}' is not approved")
        if not self.allow_paid_api:
            raise RuntimeError(
                "Paid video generation is disabled. Set ALLOW_PAID_API=true only after approving the budget."
            )
        if self.max_episode_cost_usd <= 0:
            raise RuntimeError("MAX_EPISODE_COST_USD must be a positive hard cap")
        if estimated_usd > self.max_episode_cost_usd:
            raise RuntimeError(
                f"Projected LTX cost ${estimated_usd:.2f} exceeds the episode cap "
                f"${self.max_episode_cost_usd:.2f} for {scene_count} scenes"
            )
