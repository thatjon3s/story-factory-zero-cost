from zero_cost.memory import CanonContext, SupabaseMemory, UNIVERSE_SERIES_KEY
from zero_cost.screenplay import finalize_package, validate_screenplay


def package():
    scenes = []
    state = {"door": "closed"}
    for index in range(6):
        before = dict(state)
        state = {"door": "open"} if index == 0 else dict(state)
        scenes.append({
            "duration_seconds": 8,
            "location": "Zentrale",
            "action": f"Mara und Leon untersuchen Hinweis {index}.",
            "camera": "ruhige Halbnahe",
            "lighting": "kaltes Nachtlicht",
            "dialogue": [
                {"speaker": "Mara", "emotion": "angespannt", "text": f"Das ist Hinweis Nummer {index}."},
                {"speaker": "Leon", "emotion": "ruhig", "text": f"Dann prüfen wir Spur {index}."},
            ],
            "state_before": before,
            "state_after": dict(state),
        })
    return {
        "title": "Die offene Tür",
        "description": "Eine neue Spur verändert alles.",
        "thumbnail_text": "DIE TÜR",
        "tags": ["Mystery"],
        "universe_slug": "leitungs-universum",
        "series_slug": "die-letzte-leitung",
        "character_bible": [
            {"name": "Mara", "appearance": "kurzes dunkles Haar", "wardrobe": "graue Jacke", "voice": "tiefer Alt"},
            {"name": "Leon", "appearance": "blondes Haar", "wardrobe": "blaues Hemd", "voice": "ruhiger Bariton"},
        ],
        "scenes": scenes,
        "memory_delta": {
            "episode_summary": "Mara und Leon öffnen die versiegelte Tür.",
            "canon_entries": [{
                "scope": "series", "key": "door.open", "summary": "Die versiegelte Tür ist offen.", "importance": 80
            }],
        },
    }


def test_finalized_master_is_landscape_dialogue_and_has_revision():
    result = finalize_package(package())
    assert result["aspect_ratio"] == "16:9"
    assert result["resolution"] == "1920x1080"
    assert result["has_narrator"] is False
    assert "Mara:" in result["script"]
    assert len(result["revision"]) == 16


def test_repeated_dialogue_is_rejected():
    value = finalize_package(package())
    value["scenes"][1]["dialogue"][0]["text"] = value["scenes"][0]["dialogue"][0]["text"]
    assert any("repeated dialogue" in error for error in validate_screenplay(value))


def test_memory_prompt_weights_series_as_its_own_section():
    context = CanonContext(
        universe=[{"summary": "Zeitreisen erzeugen keine Parallelwelten."}],
        series=[{"summary": "Mara kennt Leons Geheimnis."}],
        recent_episodes=[{"episode_summary": "Die Tür wurde geöffnet."}],
    )
    prompt = context.prompt_block()
    assert "UNIVERSUMS-KANON" in prompt
    assert "STARK GEWICHTETER SERIEN-KANON" in prompt
    assert UNIVERSE_SERIES_KEY == "__universe__"


def test_teaser_never_writes_canon(monkeypatch):
    memory = SupabaseMemory()
    calls = []
    monkeypatch.setattr(memory, "_request", lambda *args, **kwargs: calls.append((args, kwargs)))
    memory.commit_approved_episode({"package": {"asset_role": "promotional_teaser"}})
    assert calls == []


def test_approved_master_writes_episode_audit_and_current_canon(monkeypatch):
    value = finalize_package(package())
    memory = SupabaseMemory()
    calls = []
    monkeypatch.setattr(memory, "_request", lambda *args, **kwargs: calls.append((args, kwargs)))
    memory.commit_approved_episode({"episode_no": 1, "package": value})
    tables = [args[1] for args, _ in calls]
    assert tables == ["episode_memories", "canon_entry_revisions", "canon_entries"]
