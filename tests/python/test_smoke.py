"""Smoke test: the package and its subpackages import cleanly."""

import importlib


def test_package_imports() -> None:
    for name in (
        "evetrader",
        "evetrader.esi",
        "evetrader.data",
        "evetrader.market",
        "evetrader.advisor",
        "evetrader.tui",
    ):
        assert importlib.import_module(name) is not None
