import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict:
    return json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))


def test_manifest_declares_native_library_provider_capability():
    manifest = _manifest()

    assert "capability-pipelines.v1" in manifest["standards"]
    assert "plugin-runtime-idempotent.v1" in manifest["standards"]
    library = manifest["capabilities"]["library"]
    assert library["roles"] == ["provider"]
    assert library["operations"] == [
        "query-page",
        "query-artists",
        "query-stats",
        "tuning-names",
        "get-art",
        "sync-song",
    ]
    assert library["compatibility"] == "none"
    assert library["ownership"] == "multi-provider"
    assert library["safety"] == "safe"
    assert library["version"] == 1
    assert manifest.get("capability_api") in (None, {"standard": "capability-pipelines.v1", "version": 1})


def test_manifest_does_not_declare_private_remote_client_domain():
    manifest = _manifest()

    assert "remote-library-client" not in manifest.get("capabilities", {})
    assert "consumer" not in manifest["capabilities"]["library"]["roles"]
    assert "commands" not in manifest["capabilities"]["library"]
    assert "events" not in manifest["capabilities"]["library"]
