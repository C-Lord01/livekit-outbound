# livekit-outbound

Outbound voice calling on LiveKit, with a policy gate in front of the dialer.

Every call passes through a pre-dial authority before a SIP participant is
created. The gate applies the rules that the TCPA, the FDCPA, and
Regulation F impose on regulated outbound calling, judges the calling
window at the dialed number's own location, and writes an audit record for
every decision, ALLOW or DENY. A denied call is never placed.

## What the gate enforces

Rules run in this order and short-circuit on the first denial.

| Rule (`failed_check`) | Reason code | What it checks | Authority |
|---|---|---|---|
| `phone_number` | `NUMBER_NOT_ON_FILE` | The dialed number belongs to the contact the call is about | operational control, fails closed |
| `suppression` | `SUPPRESSED` | No stop-contact / do-not-call request is on file for the engagement | TCPA 47 CFR 64.1200(d); FDCPA 15 U.S.C. 1692c(c); Reg F 12 CFR 1006.6(c) |
| `conversation_cooldown` | `COOLDOWN_ACTIVE` | No live conversation about the engagement in the last 7 days | Reg F 12 CFR 1006.14(b)(2)(ii) |
| `seven_in_seven` | `FREQUENCY_CAP_REACHED` | Fewer than 7 attempts about the engagement in the rolling 7 days, across all of the contact's numbers | Reg F 12 CFR 1006.14(b)(2)(i) |
| `time_window` | `OUTSIDE_CALLING_WINDOW` | 8:00 AM to 9:00 PM local time at the dialed number | TCPA 47 CFR 64.1200(c)(1); Reg F 12 CFR 1006.6(b)(1)(i) |

The calling window is evaluated on the clock at the dialed number's
location, derived from its area code, never on the server's clock and not
on the contact's home timezone when the number says otherwise. Area codes
that straddle a timezone boundary (850, 812, 605, and so on) must satisfy
the window in every candidate zone. Numbers whose location cannot be
derived (toll-free, non-North-American) fall back to the contact's recorded
timezone. Daylight-saving transitions are handled by `zoneinfo`.

The rules live in `src/livekit_outbound/gate/`. The dialer adapts to the
gate's interface (`src/livekit_outbound/predial.py`); the rules are never
adapted to the dialer.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # fill in LiveKit, SIP trunk, and provider keys
uv run scripts/create_trunk.py
```

## Seed synthetic data

```bash
uv run scripts/seed_gate.py
```

Creates `./data/gate.db` (override with `GATE_DATABASE_URL`) with four
synthetic contacts. Every name is `Test Contact *` and every number sits in
the reserved fictional 555-01XX block behind an area code that matches the
contact's timezone.

| Contact | Number | Scenario | Expected gate result |
|---|---|---|---|
| A | `+13125550101`, `+13125550102` | six attempts across two numbers in the last 7 days | ALLOW once, then `FREQUENCY_CAP_REACHED` |
| B | `+12125550110` | live conversation 3 days ago | `COOLDOWN_ACTIVE` |
| C | `+12135550120` | never contacted | ALLOW (inside 8am to 9pm Pacific) |
| D | `+13035550130` | written stop-contact request on file | `SUPPRESSED` |

## Dial

```bash
uv run scripts/dial.py +13035550130 [--engagement-id N] [--now ISO-8601]
```

```
================================================================
PRE-DIAL POLICY GATE: DENY
================================================================
  reason code   SUPPRESSED
  rule          suppression: Stop-contact / do-not-call request on file
  authority     TCPA 47 CFR 64.1200(d); FDCPA 15 U.S.C. 1692c(c); Reg F 12 CFR 1006.6(c)
  inputs
    number        +13035550130
    contact       #4 Test Contact D
    engagement    #4 Synthetic outreach: renewal notice
    evaluated at  2026-07-15T19:00:00+00:00
    timezone      America/Denver (from area code 303)
    local time    2026-07-15 13:00:00 MDT in America/Denver
    window        08:00-21:00 local
    attempts      not counted (an earlier rule decided first)
  detail        suppression flag active: Contact sent a written stop-contact request
  audit         entry #2 written (DENY)
================================================================
not dialing +13035550130: SUPPRESSED (rule: suppression)
```

On ALLOW the same block prints with the list of rules passed and the
7-day attempt count, then the dialer creates the room, dispatches the
agent, records the dial as a pending call attempt in the gate database, and
creates the SIP participant.

A number the gate has never seen is enrolled as a new contact with a fresh
engagement, using the timezone derived from its area code, so a number with
no history is a clean number. A number whose location cannot be derived is
refused before the gate runs. If a contact has several engagements, pass
`--engagement-id`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | answered, agent is on the call |
| 1 | the carrier rejected or did not complete the call |
| 2 | usage or configuration error |
| 3 | the policy gate denied the call, or could not evaluate it; nothing was dialed |

## Exercising time-dependent rules outside the calling window

The gate never reads a clock. Every rule takes the instant it evaluates
against as a required argument, and the dialer is the single place the
wall clock is read. `--now` replaces that one read with an ISO-8601
instant of your choosing:

```bash
uv run scripts/dial.py +12135550120 --now 2026-07-15T07:30:00-07:00   # 07:30 Pacific: DENY
uv run scripts/dial.py +12135550120 --now 2026-07-15T12:00:00-07:00   # 12:00 Pacific: ALLOW
```

The timestamp must end in `Z` or carry an explicit offset. A naive
timestamp is rejected with a usage error rather than interpreted in an
assumed zone. The flag is accepted on the command line only; nothing reads
it from the environment, a config file, or `.env`, so it cannot leak into a
non-interactive run.

This is a test affordance, not a bypass. It moves the clock every rule
sees and nothing else: a suppressed number stays denied at any instant,
and no flag exists that skips, disables, or overrides a rule outcome. The
output carries a `CLOCK OVERRIDDEN` banner, and the audit row and any call
attempt produced this way are permanently marked (see below).

## Audit trail

Every gate evaluation writes exactly one row to `audit_log_entries`, on
every code path including a crash inside a rule (audited as DENY, then
re-raised). Three timestamps tell the story of each row:

| Column | Meaning |
|---|---|
| `decided_at` | the instant every rule evaluated against |
| `recorded_at` | wall-clock time the row was written, assigned by the database |
| `simulated_now` | the injected instant when `--now` was used; NULL for a live evaluation |

Every placed call writes a `call_attempts` row with outcome `pending`
before the SIP INVITE goes out, so the next evaluation counts it. Its
`attempted_at` is always the real wall clock, because the dial really
happened then; when the authorizing decision used `--now`, the attempt's
`simulated_now` carries the injected instant so it is visibly marked too.

## Tests

```bash
uv run pytest
```

The suite covers each rule's boundaries, the composite gate and its audit
writes, the area-code table, dialed numbers in a different timezone from
the server and from the contact record, DST transitions in both directions,
zones without DST, split area codes, the dialer adapter, and the dial
script with a fake LiveKit client. Clock injection is covered by
`tests/gate/test_clock.py` (no rule reads the wall clock, statically and at
runtime; naive instants rejected; the simulated marker) and
`tests/test_clock_override.py` (same number denied before the window and
allowed inside it, `--now` parsing, marked versus unmarked records, and
that the environment cannot inject the clock).

## Layout

```
agent.py                         the voice agent that joins the call
scripts/
  create_trunk.py                create or find the outbound SIP trunk
  seed_gate.py                   reset and seed the local gate database
  dial.py                        gate, then room, dispatch, attempt, SIP dial
src/livekit_outbound/
  predial.py                     dialer-side adapter: number -> contact/engagement -> gate
  gate/
    decide.py                    composite ALLOW/DENY, rule registry, audit write
    frequency.py                 7-in-7 rolling frequency cap
    cooldown.py                  7-day post-conversation cooldown
    time_window.py               8am to 9pm local-time window
    area_codes.py                NANP area code -> IANA timezone(s)
    clock.py                     the gate takes its instant as input; aware-datetime guard
    models.py                    SQLAlchemy schema
    db.py                        engine and session setup
    seed.py                      synthetic dataset
tests/                           pytest suite (in-memory SQLite)
docs/sdk-facts.md                LiveKit SDK ground truth for the installed versions
```
