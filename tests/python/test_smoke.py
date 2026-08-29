"""Smoke test: the package and its subpackages import cleanly."""

import importlib


def test_package_imports() -> None:
    for name in (
        "evehelper",
        "evehelper.esi",
        "evehelper.data",
        "evehelper.market",
        "evehelper.advisor",
        "evehelper.tui",
    ):
        assert importlib.import_module(name) is not None
