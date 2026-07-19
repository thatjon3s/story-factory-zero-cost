from __future__ import annotations

import os


class CostGuard:
    """Hard stop against accidentally using paid production paths."""

    def __init__(self) -> None:
        self.max_monthly_eur = float(os.getenv("MAX_EXTERNAL_MONTHLY_EUR", "0"))
        self.allow_paid_api = os.getenv("ALLOW_PAID_API", "false").lower() == "true"
        if self.max_monthly_eur != 0 or self.allow_paid_api:
            raise RuntimeError("Zero-cost policy violated: paid APIs and positive budgets are disabled")

    def require_free(self, provider: str) -> None:
        if provider not in {"ollama", "piper", "ffmpeg", "youtube", "supabase"}:
            raise RuntimeError(f"Provider '{provider}' is not approved by the zero-cost policy")
