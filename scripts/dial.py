"""Place an outbound call, if the policy gate allows it.

Usage:
    uv run scripts/dial.py +14705550123 [--engagement-id N] [--now ISO-8601]

Flow:
  0. Policy gate (livekit_outbound.gate)   -> ALLOW or DENY; an audit record is written either way.
     On DENY the script prints the reason code, the rule that fired, and the
     inputs, then exits without touching LiveKit.
  1. RoomService.create_room               -> a fresh room for this call
  2. AgentDispatchService.create_dispatch  -> explicit dispatch of AGENT_NAME with JSON metadata
  3. gate database                         -> the dial is recorded as a pending CallAttempt so the
                                              7-in-7 frequency rule sees it next time
  4. SipService.create_sip_participant     -> dial via the SIP trunk, wait_until_answered=True

--now is a test affordance for exercising time-dependent rules outside the
live calling window. It moves the clock every rule evaluates against and
nothing else: no rule is skipped or relaxed, and the audit row (and any call
attempt) is permanently marked simulated. The timestamp must carry a
trailing Z or an explicit offset; a naive timestamp is rejected. It is
accepted on the command line only and is never read from the environment,
a config file, or .env, so it cannot leak into a non-interactive run.

Exit codes:
  0  answered, agent is on the call
  1  the carrier rejected or did not complete the call
  2  usage or configuration error
  3  the policy gate denied the call, or could not evaluate it (nothing was dialed)

The gate database defaults to ./data/gate.db (see GATE_DATABASE_URL). Seed it
with scripts/seed_gate.py to get synthetic contacts covering each rule.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv
from livekit import api
from sqlalchemy.orm import Session

from livekit_outbound import predial
from livekit_outbound.gate.db import open_session
from livekit_outbound.gate.models import CallOutcome

E164 = re.compile(r"^\+[1-9]\d{6,14}$")

REQUIRED_VARS = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")

EXIT_CALL_FAILED = 1
EXIT_USAGE = 2
EXIT_GATE_DENIED = 3


def _require_env() -> None:
    missing = [n for n in REQUIRED_VARS if not os.environ.get(n, "").strip()]
    if missing:
        sys.stderr.write("Missing required environment variables: " + ", ".join(missing) + "\n")
        sys.exit(EXIT_USAGE)


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
    sys.exit(EXIT_USAGE)


def authorize_or_exit(
    gate_session: Session,
    phone_number: str,
    engagement_id: int | None,
    clock_override: datetime | None,
) -> predial.DialAuthorization:
    """Consult the policy gate. Returns only on ALLOW; every other path exits.

    A gate that cannot be evaluated is treated the same as a DENY: the call
    has no permission, so nothing is dialed. The gate audits its own
    crashes before re-raising, so the trail is intact in that case too.
    """
    try:
        authorization = predial.authorize(
            gate_session,
            phone_number,
            engagement_id=engagement_id,
            clock_override=clock_override,
        )
    except predial.PredialError as exc:
        sys.stderr.write(f"policy gate: cannot evaluate {phone_number}: {exc}\nnot dialing.\n")
        sys.exit(EXIT_GATE_DENIED)
    except Exception as exc:  # noqa: BLE001 - fail closed on any gate failure
        sys.stderr.write(
            f"policy gate: evaluation failed ({type(exc).__name__}: {exc})\nnot dialing.\n"
        )
        sys.exit(EXIT_GATE_DENIED)

    print(predial.format_authorization(authorization))
    sys.stdout.flush()  # keep the block ahead of the stderr summary when piped
    if not authorization.allowed:
        sys.stderr.write(
            f"not dialing {phone_number}: {authorization.reason_code} "
            f"(rule: {authorization.rule_name})\n"
        )
        sys.exit(EXIT_GATE_DENIED)
    return authorization


async def dial(
    phone_number: str,
    engagement_id: int | None = None,
    clock_override: datetime | None = None,
) -> None:
    load_dotenv()
    _require_env()

    gate_session = open_session()
    try:
        authorization = authorize_or_exit(gate_session, phone_number, engagement_id, clock_override)
        await _place_call(gate_session, authorization, phone_number)
    finally:
        gate_session.close()


async def _place_call(
    gate_session: Session, authorization: predial.DialAuthorization, phone_number: str
) -> None:
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

        # The agent needs the gate subject (contact, engagement) so that a
        # cessation during the call can be written against the same
        # engagement the pre-dial suppression rule reads.
        metadata = json.dumps(
            {
                "phone_number": phone_number,
                "participant_identity": participant_identity,
                "contact_id": authorization.contact.id,
                "engagement_id": authorization.engagement.id,
            }
        )
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=agent_name, room=room_name, metadata=metadata)
        )
        print(f"dispatch: {dispatch.id} (agent '{agent_name}')")

        # The gate counts calls placed, not calls answered: record the
        # attempt before the SIP INVITE goes out so a concurrent or
        # immediately-following evaluation already sees it.
        attempt = predial.record_attempt(gate_session, authorization, external_call_id=room_name)
        print(
            f"gate: recorded call attempt #{attempt.id} (pending) "
            f"for engagement #{authorization.engagement.id}"
        )
        if authorization.simulated:
            print(
                "gate: !! attempt marked simulated (clock overridden); attempted_at is the "
                f"wall clock {attempt.attempted_at.isoformat()}"
            )

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
            predial.mark_attempt(gate_session, attempt, CallOutcome.FAILED)
            sys.stderr.write(
                f"call failed: SIP {e.sip_status_code} {e.sip_status or ''} ({e.message})\n"
            )
            sys.exit(EXIT_CALL_FAILED)

        print(f"answered: participant {info.participant_identity} sid={info.participant_id}")
        print(
            "The agent is now on the call. It hangs up when the person says goodbye "
            "or asks it to; you can also hang up from the phone."
        )
    finally:
        await lkapi.aclose()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dial.py",
        description="Place an outbound call through the policy gate.",
    )
    parser.add_argument("phone_number", help="E.164 number to dial, e.g. +14705550123")
    parser.add_argument(
        "--engagement-id",
        type=int,
        default=None,
        help="Which of the contact's engagements this call is about "
        "(required only when they have several)",
    )
    parser.add_argument(
        "--now",
        metavar="ISO-8601",
        default=None,
        help="Evaluate every gate rule at this instant instead of the wall clock, "
        "e.g. 2026-07-15T12:30:00Z or 2026-07-15T08:30:00-04:00. Must include Z or "
        "an offset. Test affordance only: no rule is skipped, and the audit record "
        "is marked simulated.",
    )
    args = parser.parse_args(argv)
    if not E164.match(args.phone_number):
        parser.error(f"{args.phone_number!r} is not an E.164 number (e.g. +14705550123)")
    args.clock_override = None
    if args.now is not None:
        try:
            args.clock_override = predial.parse_instant(args.now)
        except ValueError as exc:
            parser.error(f"--now: {exc}")
    return args


def main() -> None:
    args = parse_args(sys.argv[1:])
    asyncio.run(dial(args.phone_number, args.engagement_id, args.clock_override))


if __name__ == "__main__":
    main()
