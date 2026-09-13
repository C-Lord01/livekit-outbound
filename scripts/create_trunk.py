"""Create (or find) the LiveKit outbound SIP trunk that points at Twilio.

Reads every value from the environment (a .env file in the project root is
loaded if present). Prints the trunk ID on stdout. Idempotent: if a trunk with
the same name already exists, its ID is printed and nothing is created.

API surface used (see docs/sdk-facts.md):
  livekit.api.LiveKitAPI().sip.list_outbound_trunk / create_outbound_trunk
  livekit.api.SIPOutboundTrunkInfo, CreateSIPOutboundTrunkRequest,
  ListSIPOutboundTrunkRequest, SIPTransport
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from livekit import api

REQUIRED_VARS = (
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "TWILIO_SIP_TERMINATION_URI",
    "TWILIO_SIP_USERNAME",
    "TWILIO_SIP_PASSWORD",
    "TWILIO_PHONE_NUMBER",
)


def _example_values() -> dict[str, str]:
    """Placeholder values from .env.example, so unedited copies are rejected."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    if not example.exists():
        return {}
    return {k: v for k, v in dotenv_values(example).items() if v}


def _require_env() -> dict[str, str]:
    placeholders = _example_values()
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in REQUIRED_VARS:
        value = os.environ.get(name, "").strip()
        if not value or value == placeholders.get(name):
            missing.append(name)
        values[name] = value
    if missing:
        sys.stderr.write(
            "Missing or still-placeholder environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in real values.\n"
        )
        sys.exit(2)
    return values


async def find_trunk_by_name(lkapi: api.LiveKitAPI, name: str) -> api.SIPOutboundTrunkInfo | None:
    resp = await lkapi.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
    for trunk in resp.items:
        if trunk.name == name:
            return trunk
    return None


async def main() -> None:
    load_dotenv()
    env = _require_env()
    trunk_name = os.environ.get("LIVEKIT_SIP_TRUNK_NAME", "twilio-outbound").strip()

    address = env["TWILIO_SIP_TERMINATION_URI"]
    if address.startswith("sip:"):
        address = address[len("sip:") :]

    lkapi = api.LiveKitAPI(
        url=env["LIVEKIT_URL"],
        api_key=env["LIVEKIT_API_KEY"],
        api_secret=env["LIVEKIT_API_SECRET"],
    )
    try:
        existing = await find_trunk_by_name(lkapi, trunk_name)
        if existing is not None:
            sys.stderr.write(f"Trunk '{trunk_name}' already exists; reusing it.\n")
            print(existing.sip_trunk_id)
            return

        trunk = api.SIPOutboundTrunkInfo(
            name=trunk_name,
            address=address,
            transport=api.SIPTransport.SIP_TRANSPORT_AUTO,
            numbers=[env["TWILIO_PHONE_NUMBER"]],
            auth_username=env["TWILIO_SIP_USERNAME"],
            auth_password=env["TWILIO_SIP_PASSWORD"],
            metadata=os.environ.get("TWILIO_TRUNK_SID", "").strip(),
        )
        created = await lkapi.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(trunk=trunk)
        )
        sys.stderr.write(f"Created trunk '{trunk_name}' -> {address}\n")
        print(created.sip_trunk_id)
    finally:
        await lkapi.aclose()


if __name__ == "__main__":
    asyncio.run(main())
