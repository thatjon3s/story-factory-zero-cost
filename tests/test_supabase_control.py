from zero_cost.supabase_control import SupabaseControlPlane


def control_with(responder):
    control = object.__new__(SupabaseControlPlane)
    control.owner_id = "owner"
    control._request = responder
    return control


def test_control_plane_reads_episode_from_supabase():
    control = control_with(lambda method, table, **kwargs: [{"id": 7, "status": "idea"}])
    assert control.get(7)["id"] == 7


def test_optimistic_transition_rejects_empty_update_result():
    control = control_with(lambda method, table, **kwargs: [])
    try:
        control.update(7, {"status": "producing"}, expected="idea")
    except RuntimeError as exc:
        assert "invalid transition" in str(exc)
    else:
        raise AssertionError("missing optimistic update result must fail")


def test_schema_moves_operational_state_out_of_github():
    schema = open("supabase/schema.sql", encoding="utf-8").read()
    assert "public.episodes" in schema
    assert "public.automation_events" in schema
    assert "public.scene_jobs" in schema
