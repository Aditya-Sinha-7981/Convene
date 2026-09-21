import pytest

from server.db import Database
from tests.support.server import settings_in, start_server


@pytest.fixture
def db(tmp_path):
    """A fresh, migrated database in a temporary file (never the real data/convene.db)."""
    database = Database.open(tmp_path / "convene.db")
    yield database
    database.close()


@pytest.fixture
def settings(tmp_path):
    return settings_in(tmp_path)


@pytest.fixture
async def server(settings):
    """The real Convene app on a loopback port, with a temporary database."""
    running = await start_server(settings)
    yield running
    await running.stop()


@pytest.fixture
async def make_phone(server):
    """Factory for synthetic phones registered against a meeting; all are closed at teardown."""
    made = []

    def make(meeting_id, **kwargs):
        from tests.support.synthetic_phone import SyntheticPhone
        phone = SyntheticPhone(server.base_url, meeting_id, **kwargs)
        made.append(phone)
        return phone

    yield make
    for phone in made:
        await phone.close()
