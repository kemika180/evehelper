"""Config model: construction, validation bounds, immutability, extra-field ban."""

import pytest
from pydantic import ValidationError

from evetrader.config import Config


def _valid_config() -> Config:
    return Config(
        esi_client_id="abc123",
        contact="jane@example.com",
    )


def test_valid_config_constructs() -> None:
    config = _valid_config()
    assert config.esi_client_id == "abc123"
    assert config.theme == "kemika-purple"  # default applied


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_callback_port_must_be_a_valid_port(port: int) -> None:
    with pytest.raises(ValidationError):
        Config(esi_client_id="abc123", contact="jane@example.com", callback_port=port)


def test_config_is_frozen() -> None:
    config = _valid_config()
    with pytest.raises(ValidationError):
        config.contact = "someone-else@example.com"  # type: ignore[misc]


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Config(
            esi_client_id="abc123",
            contact="jane@example.com",
            unexpected=True,  # type: ignore[call-arg]
        )
