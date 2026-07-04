from __future__ import annotations

import json

from remote_library_client.store import RemoteLibraryClientStore


def test_save_writes_settings_atomically(tmp_path):
    store = RemoteLibraryClientStore(tmp_path)

    saved = store.save({"sources": [{"providerId": "one"}]})

    assert saved == {"sources": [{"providerId": "one"}]}
    assert json.loads(store.settings_path.read_text()) == saved
    assert not list(store.root.glob("*.tmp"))


def test_upsert_and_remove_serialize_full_read_modify_write(tmp_path):
    store = RemoteLibraryClientStore(tmp_path)

    store.upsert_source({"providerId": "one", "enabled": True})
    store.upsert_source({"providerId": "two", "enabled": False})
    store.upsert_source({"providerId": "one", "enabled": False})

    assert store.list_sources() == [
        {"providerId": "two", "enabled": False},
        {"providerId": "one", "enabled": False},
    ]
    assert store.remove_source("two") is True
    assert store.remove_source("missing") is False
    assert store.list_sources() == [{"providerId": "one", "enabled": False}]