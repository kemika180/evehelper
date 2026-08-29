"""CharacterStore adds/dedups/removes characters and persists across instances."""

from pathlib import Path

from evehelper.session import CharacterRecord, CharacterStore


def test_add_dedups_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "characters.json"
    store = CharacterStore(path)
    assert store.records() == []

    store.add(CharacterRecord(1, "Alice"))
    store.add(CharacterRecord(2, "Bob"))
    store.add(CharacterRecord(1, "Alice Renamed"))  # same id -> update, not duplicate

    reloaded = CharacterStore(path)
    records = reloaded.records()
    assert [r.character_id for r in records] == [2, 1]
    assert {r.character_id: r.name for r in records}[1] == "Alice Renamed"


def test_remove(tmp_path: Path) -> None:
    path = tmp_path / "characters.json"
    store = CharacterStore(path)
    store.add(CharacterRecord(1, "Alice"))
    store.add(CharacterRecord(2, "Bob"))

    store.remove(1)

    assert [r.character_id for r in CharacterStore(path).records()] == [2]
