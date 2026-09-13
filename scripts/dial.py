"""Place an outbound call: create a room, dispatch the agent, dial the number.

Usage:
    uv run scripts/dial.py +14705551234

Flow (docs/sdk-facts.md sections 1, 2, 4):
  1. RoomService.create_room            -> a fresh room for this call
  2. AgentDispatchService.create_dispatch -> explicit dispatch of AGENT_NAME with JSON metadata
  3. SipService.create_sip_participant  -> dial via the Twilio trunk, wait_until_answered=True
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from datetime import timedelta

from dotenv import load_dotenv
from livekit import api

E164 = re.compile(r"^\+[1-9]\d{6,14}$")

REQUIRED_VARS = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")


def _require_env() -> None:
    missing = [n for n in REQUIRED_VARS if not os.environ.get(n, "").strip()]
    if missing:
        sys.stderr.write("Missing required environment variables: " + ", ".join(missing) + "\n")
        sys.exit(2)


async def resolve_trunk_id(lkapi: api.LiveKitAPI) -> str:
    trunk_id = os.environ.get("SIP_OUTBOUND_TRUNK_ID", "").strip()
    if trunk_id:
        return trunk_id
    name = os.environ.get("LIVEKIT_SIP_TRUNK_NAME", "twilio-outbound").strip()
    resp = await lkapi.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
    for trunk in resp.items:
        if trunk.name == name:
            return trunk.sip_trunk_id
    sys.stderr.write(
        f"No outbound trunk named '{name}' found and SIP_OUTBOUND_TRUNK_ID is unset. "
        "Run scripts/create_trunk.py first.\n"
    )
    sys.exit(2)


async def dial(phone_number: str) -> None:
    load_dotenv()
    _require_env()

    agent_name = os.environ.get("AGENT_NAME", "outbound-caller").strip()
    room_name = f"call-{uuid.uuid4().hex[:12]}"
    participant_identity = f"phone-{phone_number.lstrip('+')}"

    lkapi = api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        trunk_id = await resolve_trunk_id(lkapi)

        room = await lkapi.room.create_room(
            api.CreateRoomRequest(name=room_name, empty_timeout=60, max_participants=2)
        )
        print(f"room: {room.name}")

        metadata = json.dumps(
            {"phone_number": phone_number, "participant_identity": participant_identity}
        )
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=agent_name, room=room_name, metadata=metadata)
        )
        print(f"dispatch: {dispatch.id} (agent '{agent_name}')")

        print(f"dialing {phone_number} via trunk {trunk_id} ...")
        request = api.CreateSIPParticipantRequest(
            sip_trunk_id=trunk_id,
            sip_call_to=phone_number,
            room_name=room_name,
            participant_identity=participant_identity,
            participant_name="Phone callee",
            wait_until_answered=True,
            krisp_enabled=True,
        )
        request.ringing_timeout.FromTimedelta(timedelta(seconds=45))
        request.max_call_duration.FromTimedelta(timedelta(minutes=10))

        try:
            info = await lkapi.sip.create_sip_participant(request)
        except api.SipCallError as e:
            sys.stderr.write(
                f"call failed: SIP {e.sip_status_code} {e.sip_status or ''} ({e.message})\n"
            )
            sys.exit(1)

        print(f"answered: participant {info.participant_identity} sid={info.participant_id}")
        print("The agent is now on the call. Hang up from the phone to end it.")
    finally:
        await lkapi.aclose()


def main() -> None:
    if len(sys.argv) != 2 or not E164.match(sys.argv[1]):
        sys.stderr.write("usage: dial.py +E164_PHONE_NUMBER   (e.g. +14705551234)\n")
        sys.exit(2)
    asyncio.run(dial(sys.argv[1]))


if __name__ == "__main__":
    main()
