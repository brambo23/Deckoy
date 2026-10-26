import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def captured():
    """The decoded Elgato session: list of (stream, from_dock, time, Frame)."""
    from deckoy.protocol.frame import Frame
    out = []
    for line in (FIXTURES / "session20001.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        f = line.split(" ")
        out.append((f[0], f[1] == "DOCK", float(f[2]),
                    Frame(int(f[3], 16), int(f[4]), int(f[5]), bytes.fromhex(f[6]) if len(f) > 6 else b"")))
    return out
