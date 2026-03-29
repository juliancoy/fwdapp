import pytest
import jwt as pyjwt
import os
# Use unconditional assignment so env vars are set before server.py is imported
# (server.py reads them at module level; setdefault would be unsafe if env was pre-set)
os.environ["APP_JWT_SECRET"]   = "test-secret"
os.environ["APP_JWT_ISSUER"]   = "my-thin-bridge"
os.environ["APP_JWT_AUDIENCE"] = "my-attached-app"

from server import _validate_jwt, Room, Member, Motion

SECRET = "test-secret"

def make_token(sub="user1", name="Alice", exp_offset=3600):
    import time
    return pyjwt.encode(
        {"sub": sub, "name": name, "iss": "my-thin-bridge",
         "aud": "my-attached-app", "exp": int(time.time()) + exp_offset},
        SECRET, algorithm="HS256"
    )

def test_validate_jwt_valid():
    token = make_token()
    claims = _validate_jwt(token)
    assert claims["sub"] == "user1"

def test_validate_jwt_expired():
    token = make_token(exp_offset=-10)
    with pytest.raises(Exception):
        _validate_jwt(token)

def test_validate_jwt_wrong_audience():
    import time
    bad = pyjwt.encode(
        {"sub": "x", "iss": "my-thin-bridge", "aud": "wrong", "exp": int(time.time()) + 3600},
        SECRET, algorithm="HS256"
    )
    with pytest.raises(Exception):
        _validate_jwt(bad)

def test_room_to_dict_no_motion():
    from server import _room_to_dict
    room = Room(room_id="r1")
    room.members.append(Member(id="u1", name="Alice", is_chair=True))
    d = _room_to_dict(room)
    assert d["room_id"] == "r1"
    assert d["phase"] == "open"
    assert d["motion"] is None
    assert d["members"][0]["is_chair"] is True

def test_get_member_found():
    from server import _get_member
    room = Room(room_id="r1")
    room.members.append(Member(id="u1", name="Alice"))
    assert _get_member(room, "u1").name == "Alice"

def test_get_member_not_found():
    from server import _get_member
    room = Room(room_id="r1")
    assert _get_member(room, "nope") is None

def test_is_chair():
    from server import _is_chair
    room = Room(room_id="r1")
    room.members.append(Member(id="u1", name="Alice", is_chair=True))
    room.members.append(Member(id="u2", name="Bob"))
    assert _is_chair(room, "u1") is True
    assert _is_chair(room, "u2") is False

import pytest_asyncio

@pytest.mark.asyncio
async def test_advance_speaker_no_queue():
    import server as srv
    srv.rooms.clear(); srv.connections.clear(); srv._timer_tasks.clear()
    room = Room(room_id="r1", phase="floor_held",
                current_speaker="u1", speaker_queue=[])
    room.members.append(Member(id="u1", name="Alice"))
    srv.rooms["r1"] = room
    srv.connections["r1"] = {}
    await srv._advance_speaker("r1")
    assert room.phase == "open"
    assert room.current_speaker is None

@pytest.mark.asyncio
async def test_advance_speaker_with_queue():
    import server as srv
    srv.rooms.clear(); srv.connections.clear(); srv._timer_tasks.clear()
    room = Room(room_id="r2", phase="floor_held",
                current_speaker="u1", speaker_queue=["u2"],
                speaker_time=60)
    room.members += [Member(id="u1", name="Alice"), Member(id="u2", name="Bob")]
    srv.rooms["r2"] = room
    srv.connections["r2"] = {}
    await srv._advance_speaker("r2")
    assert room.current_speaker == "u2"
    assert room.phase == "floor_held"
    assert room.timer_remaining == 60
    # Cancel the new timer so it doesn't run during test
    srv._cancel_task(srv._timer_tasks, "r2")

@pytest.mark.asyncio
async def test_advance_speaker_guard_double_call():
    import server as srv
    srv.rooms.clear(); srv.connections.clear(); srv._timer_tasks.clear()
    room = Room(room_id="r3", phase="open", current_speaker=None)
    srv.rooms["r3"] = room
    srv.connections["r3"] = {}
    # Should be a no-op
    await srv._advance_speaker("r3")
    assert room.phase == "open"
