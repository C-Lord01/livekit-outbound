# Native Outbound Is the Business, and Compliance Is the Gate

**An outside point of view on LiveKit Telephony**
Chris Kulpa — September 2026

---

## Where this comes from

I've spent the last several years on the enterprise side of phone-based agents: integrating Five9 and Acqueon into an in-house CRM, running outbound at volume under TCPA and Reg F, and before that doing carrier-side product work on Australia's NBN rollout. I'm the customer LiveKit Telephony is trying to win, and I've been the one holding the pager when a trunk misbehaves at 8am.

I read the JD's invitation literally: try the product, form an opinion, bring it. So I spent a weekend building on LiveKit SIP and the Agents framework, and this is what I think.

## The observation

LiveKit Phone Numbers is inbound-only and US-only today. Outbound and international are on the roadmap.

That means the highest-value use cases named in LiveKit's own positioning — outbound sales, appointment reminders, collections, healthcare outreach — are the ones native telephony cannot serve. Every one of those enterprises is on a bring-your-own trunk from Twilio, Telnyx, or Plivo.

Three consequences, in order of how much they cost:

**1. The margin.** Outbound-heavy customers are the highest-minute customers on the platform. When they BYO the trunk, the per-minute telephony revenue goes to the trunk provider and LiveKit keeps only session minutes and inference. Native is inbound-only, so LiveKit captures the least telephony margin on exactly the traffic that generates the most minutes.

**2. The quality story.** The argument for going native is fewer hops, better audio, quality you can tune. That argument doesn't reach outbound calls at all right now, and outbound is where audio quality is most commercially load-bearing — a bad first two seconds on an inbound call annoys a customer who already wanted to talk to you; a bad first two seconds outbound is a hangup.

**3. The competitive narrative.** Telnyx and Plivo are both running comparison pages against LiveKit right now, and they aren't arguing about media. They're arguing carrier capabilities: attestation, transfers, trunk configuration depth, country coverage, in-country media routing. That's a winnable fight, but only on their terms, not on media-stack terms.

## Why outbound is harder than inbound, and it isn't media

The hard part of shipping native outbound isn't SIP or RTP. LiveKit already does that well. The hard part is that **the moment LiveKit originates outbound traffic, it becomes the originating service provider.** That brings obligations that don't exist for inbound:

- STIR/SHAKEN signing and attestation-level responsibility
- Robocall Mitigation Database registration and a documented mitigation program
- Traceback response obligations when a campaign gets flagged
- Exposure when a customer dials in a way that gets LiveKit's number ranges tagged as Scam Likely, degrading delivery for every other customer on the same ranges

This is the thing I'd want the team to internalize early: on outbound, **customer compliance failures become LiveKit's delivery problem.** Reputation damage on shared number ranges is a multi-tenant blast radius. That makes compliance a platform concern, not an application concern, and it's the strongest reason to build it into the trunk rather than leave it to each customer's Python.

Right now every serious outbound team on LiveKit is reimplementing the same four things in application code, badly and unauditably: suppression lists, calling-window checks, attempt-frequency counters, and cease-and-desist handling. I know because I built that layer myself this weekend and there was nothing to build on.

## Four proposals, in priority order

**1. A-level attestation as the native outbound headline.**
Because LiveKit owns the number when it's a LiveKit number, it can sign at full attestation by default. A BYO-trunk customer generally cannot get that guarantee without work. This is the single clearest reason a developer should buy the number from LiveKit rather than bolt on Twilio, and it's a reason phrased in the currency outbound buyers actually care about: answer rate. I'd lead the native outbound launch with it rather than with setup convenience.

**2. Outbound policy primitives at the trunk.**
Suppression lists, per-destination calling windows, and attempt counters, enforced at `CreateSIPParticipant` and surfaced in the audit trail. Deny with a structured reason code, don't fail silently.

There's a specific technical reason this belongs in the platform rather than in customer code. Calling windows have to resolve against the dialled number's local time. From application code, the only available signal is the area code — so that's what I built, with DST boundaries, split area codes, and server-timezone independence all tested. It is still structurally wrong, and no amount of care fixes it: number portability means a 212 mobile can live in California permanently. The carrier knows the real routing and the line type. The application never can. Every customer building this in Python is building the same defensible-but-incorrect thing.

There's also a signal that LiveKit has already made half this call. Agent session logs tag transcripts as `lk.pii.*`. The platform already classifies call content as regulated data. It just doesn't yet offer anything that acts on that classification.

**3. Regulatory footing as the gate on international expansion, not an afterthought.**
"Buy a number in two clicks" is the easy half. "Buy a number you can legally dial out from" is the moat. Country sequencing should be driven by outbound regulatory tractability, not just demand: UK/Ofcom and Australia/ACMA and the Do Not Call Register are relatively clean; EU ePrivacy consent varies materially by member state; India requires in-country media routing, which Plivo already markets against LiveKit on. I'd want the roadmap to state which countries LiveKit will support for *outbound* versus inbound-only, because those are different products and treating them as one number will produce a bad quarter eventually.

**4. Price the compliance tier.**
The HIPAA-on-Scale precedent already establishes that regulated capability gates a tier. Outbound policy enforcement, attestation guarantees, and retained audit trails are the same shape of product and should carry the same shape of pricing. This also aligns incentives correctly: the customers who most need the guardrails are the ones running the volume that creates the reputation risk.

## What I built

A pre-dial policy gate on LiveKit SIP and the Agents framework, dialling through a Twilio elastic trunk, to see where the seams are. Three checks run before a SIP participant is ever created:

- **Attempt frequency** — a 7-in-7 rule aggregated across every number associated with a contact, not per-number
- **Calling window** — 8am–9pm resolved against the dialled number's local time
- **Post-conversation cooldown** — triggered only on a confirmed live conversation, using the Agents SDK's answering-machine detection to separate a live answer from voicemail or an IVR

Then one thing that only works because of LiveKit's architecture: **mid-call cessation.** When the person says stop calling me or names an attorney, the agent detects it on the final transcript, writes a permanent suppression, acknowledges, and ends the call — in that order, because the suppression has to be durable before teardown begins.

Measured on live calls to my own phone, detection to durable suppression completed in **under 45 milliseconds** across both recorded runs (39ms and 42ms). A conservative phrase list handles unambiguous cases without a model call; everything else goes to a classifier with a 2.5-second budget against a measured p95 of 1.6s, and any timeout, error, or ambiguous verdict fails closed to suppression. The asymmetry justifies it: a wrongly-suppressed contact costs one conversation, a missed cessation is a statutory violation.

On a fire-and-forget REST dialler, none of that is possible — cessation becomes a cleanup job that runs after the violation has already happened. That's a genuine platform advantage I don't think is currently being told as a story.

Repo and a three-minute demo are linked with this note.

## Smaller things I hit

- Answering-machine detection is fully available in `livekit-agents` 1.8.1 — five categories, LLM-based, with a clean gate on call status so ringback doesn't poison the classification. The product marketing page still lists it as coming soon. A developer reading the page would conclude a shipped feature doesn't exist.
- `TransferSipParticipant` isn't supported on LiveKit Phone Numbers yet. Warm transfer to a human is table stakes for any contact centre evaluation, so this reads as a bigger gap than it probably is internally.
- Outbound trunk setup is meaningfully harder than inbound: two configuration steps versus eight, across two vendors instead of one. Inbound native is genuinely a couple of clicks. The gap between them is where a first-time developer forms their opinion of the product.
- Two agent-lifecycle edges worth documenting: removing a participant via the API doesn't trigger close-on-disconnect, and a function tool can't await its own speech handle or the session close without deadlocking. Both are discoverable only by reading the SDK source.

## Where I could be wrong

I'm working from public docs, a weekend of building, and my own operator experience. Things I'd want to test before committing any of the above to a roadmap:

- What share of current telephony minutes is outbound versus inbound, and what share of outbound customers would move to native numbers if outbound shipped? If most are staying on existing carrier contracts regardless, proposal 1 matters more than proposal 2 and the sequencing flips.
- Whether compliance is actually a stated blocker in lost deals, or whether enterprises assume they own it and would see platform enforcement as LiveKit getting in the way. I believe the former; I'd want to hear five customers say it before betting a quarter on it.
- Whether the attestation story survives contact with how number ranges are actually provisioned at LiveKit's upstream carriers.

If any of those cut the other way, I'd change my mind and say so. That's the part of the job I'd expect to spend the first month on.

---

*Happy to go deeper on any of this, including the parts where I'm probably wrong.*
