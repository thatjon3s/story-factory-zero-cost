import pytest

from zero_cost.cost_guard import CostGuard


def test_paid_video_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_PAID_API", raising=False)
    monkeypatch.delenv("MAX_EPISODE_COST_USD", raising=False)
    with pytest.raises(RuntimeError, match="disabled"):
        CostGuard().authorize_paid_episode("ltx", 4.80, 8)


def test_episode_cap_is_enforced(monkeypatch):
    monkeypatch.setenv("ALLOW_PAID_API", "true")
    monkeypatch.setenv("MAX_EPISODE_COST_USD", "4")
    with pytest.raises(RuntimeError, match="exceeds"):
        CostGuard().authorize_paid_episode("ltx", 4.80, 8)


def test_episode_inside_cap_is_authorized(monkeypatch):
    monkeypatch.setenv("ALLOW_PAID_API", "true")
    monkeypatch.setenv("MAX_EPISODE_COST_USD", "7")
    CostGuard().authorize_paid_episode("ltx", 4.80, 8)
