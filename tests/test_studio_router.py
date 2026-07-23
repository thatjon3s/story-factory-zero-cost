from pathlib import Path

from zero_cost.studio_router import StudioLease, StudioRouter


class FakeQueue:
    def __init__(self, lease=None):
        self.lease = lease
        self.events = []

    def claim(self, revision):
        self.events.append(("claim", revision))
        return self.lease

    def mark_rendering(self, lease):
        self.events.append(("rendering", lease.job["id"]))

    def upload_scene(self, revision, scene_index, source):
        self.events.append(("upload", revision, scene_index, source.name))
        return f"{revision}/scene-{scene_index:02d}.mp4"

    def complete(self, lease, output_url, quality_report):
        self.events.append(("complete", output_url, quality_report["passed"]))

    def retry(self, lease, error):
        self.events.append(("retry", error))


class FakeAdapter:
    def generate(self, scene, package, destination: Path):
        destination.write_bytes(b"video")


def package():
    return {
        "revision": "0123456789abcdef",
        "scenes": [{"duration_seconds": 8}],
    }


def lease(adapter_key="fake"):
    return StudioLease(
        job={
            "id": "job-1",
            "scene_index": 0,
            "leased_by": "worker-1",
            "payload": {"scene": {"duration_seconds": 8}},
        },
        provider={"adapter_key": adapter_key},
    )


def test_router_only_uses_selected_installed_adapter(monkeypatch, tmp_path):
    queue = FakeQueue(lease())
    monkeypatch.setattr(
        "zero_cost.studio_router.inspect_scene",
        lambda path: {"passed": True, "errors": []},
    )
    result = StudioRouter({"fake": FakeAdapter()}, queue).work_once(package(), tmp_path)
    assert result is not None
    assert ("claim", "0123456789abcdef") in queue.events
    assert any(event[0] == "complete" for event in queue.events)


def test_missing_adapter_returns_job_to_retry(tmp_path):
    queue = FakeQueue(lease("not-installed"))
    try:
        StudioRouter({}, queue).work_once(package(), tmp_path)
    except RuntimeError as exc:
        assert "No installed studio adapter" in str(exc)
    else:
        raise AssertionError("missing adapter must fail")
    assert any(event[0] == "retry" for event in queue.events)


def test_schema_requires_zero_cost_commercial_automation():
    schema = Path("supabase/schema.sql").read_text(encoding="utf-8")
    assert "incremental_cost_usd = 0" in schema
    assert "commercial_use_allowed" in schema
    assert "automation_allowed" in schema
    assert "for update skip locked" in schema.lower()
