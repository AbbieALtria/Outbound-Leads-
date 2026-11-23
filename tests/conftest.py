"""Test-wide isolation.

db.py resolves DATA_DIR at import, and importing app.py initialises the database
there and writes the boot counter. Pointing DATA_DIR at a temp folder before
anything is imported keeps every test off the real leads.db, and clearing the
provider keys keeps every paid integration dormant (tests mock the rest)."""
import os
import pathlib
import socket
import sys
import tempfile

# No test may reach the network: every provider is mocked, and CI runs with no
# secrets. Any attempt to open an outbound connection fails loudly here, so a
# test that silently depended on a live API cannot pass locally and fail in CI.


def _blocked_connect(self, address):
    raise RuntimeError(f"tests must not use the network (tried to connect to {address!r})")


socket.socket.connect = _blocked_connect
socket.socket.connect_ex = lambda self, address: (_ for _ in ()).throw(
    RuntimeError(f"tests must not use the network (tried to connect to {address!r})"))

_tmp = tempfile.mkdtemp(prefix="seo_leads_tests_")
os.environ["DATA_DIR"] = _tmp
for var in ("DATABASE_URL", "EMAIL_VERIFY_API_KEY", "EMAIL_VERIFY_PROVIDER",
            "APOLLO_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(var, None)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_legacy_imports(monkeypatch):
    """On an empty leads table init_db imports the project's own leads_*.csv and
    old seen-leads file (keyed on the script folder, not DATA_DIR). Point both
    at the temp folder so a test database starts genuinely empty."""
    import db
    monkeypatch.setattr(db, "SCRIPT_DIR", pathlib.Path(_tmp))
    monkeypatch.setattr(db, "OLD_SEEN_DB", pathlib.Path(_tmp) / "absent.sqlite3")
