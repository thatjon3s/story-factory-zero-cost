from zero_cost.puppet_renderer import DialogueCue, _active_cue, _palette, _wrap


def test_character_palette_is_stable_and_distinct():
    assert _palette("Mara") == _palette("Mara")
    assert _palette("Mara") != _palette("Leon")


def test_dialogue_cue_selects_only_active_speaker():
    cues = [DialogueCue("Mara", "Hallo", "ruhig", 0.2, 1.2)]
    assert _active_cue(cues, 0.1) is None
    assert _active_cue(cues, 0.8).speaker == "Mara"
    assert _active_cue(cues, 1.2) is None


def test_subtitles_stay_compact():
    assert len(_wrap("Das ist ein absichtlich längerer Satz für eine gut lesbare Dialogzeile.", 28)) <= 2
