"""D5 review: investigate never restarts over a saved ended session."""

from types import SimpleNamespace

import pytest

from fleetsre.sre.investigator import investigate
from fleetsre.sre.session import MemorySessionStore, Session


@pytest.mark.parametrize("status", ["submitted", "needs_input"])
def test_investigate_on_ended_session_raises(status: str):
    store = MemorySessionStore()
    store.save(Session(
        incident_id="inc-x", status=status, reason="propose",
        transcript_id="tx-inc-x", turn=9,
    ))
    incident = SimpleNamespace(id="inc-x")
    with pytest.raises(ValueError):
        investigate(incident, None, None, store)
    assert store.load("inc-x").turn == 9
