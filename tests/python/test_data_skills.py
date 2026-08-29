"""The bundled skills reference loads and carries the expected static facts."""

from evehelper.data.skills import load_skills


def test_load_skills_reads_the_bundled_file() -> None:
    skills = load_skills()
    assert len(skills) > 400  # the full published skill list

    accounting = skills[16622]
    assert accounting.name == "Accounting"
    assert accounting.group == "Trade"
    assert accounting.rank == 3
    assert accounting.primary == "Charisma"
    assert accounting.secondary == "Memory"
    assert accounting.description  # non-empty description text


def test_load_skills_is_cached() -> None:
    assert load_skills() is load_skills()  # same object, loaded once
