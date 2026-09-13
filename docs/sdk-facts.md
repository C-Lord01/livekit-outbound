# LiveKit SDK ground truth (installed versions)

Generated 2026-09-12 by inspecting the packages installed in this project's venv.
Every claim below cites a real file under `.venv/Lib/site-packages/`. Line numbers
refer to those exact files. Signatures were also confirmed at runtime with
`inspect.signature` on the imported objects.

Project: `C:\Users\Chris\livekit-outbound` (uv, Python 3.12.9)
Site-packages root (abbreviated `SP` below): `.venv/Lib/site-packages`

## Installed livekit* package versions

| Package | Version |
|---|---|
| livekit | 1.1.18 |
| livekit-agents | 1.8.1 |
| livekit-api | 1.2.1 |
| livekit-protocol | 1.1.26 |
| livekit-plugins-cartesia | 1.8.1 |
| livekit-plugins-deepgram | 1.8.1 |
| livekit-plugins-openai | 1.8.1 |
| livekit-plugins-silero | 1.8.1 |
| livekit-blingfire | 1.1.0 (transitive dep) |
| livekit-local-inference | 0.2.7 (transitive dep) |

Source: `uv pip list | grep -i livekit`.

---

## 1. Creating an outbound SIP participant

**Import paths**

- Request message: `from livekit.api import CreateSIPParticipantRequest`
  - Re-exported via `from livekit.protocol.sip import *` in `SP/livekit/api/__init__.py:35`.
  - Defined in `SP/livekit/protocol/sip.pyi:854` (class), constructor at `sip.pyi:924`.
- Service: `SipService` is **not** exported from `livekit.api` top-level. Reach it either
  through `livekit.api.LiveKitAPI().sip` (`SP/livekit/api/livekit_api.py:143-145`) or
  `from livekit.api.sip_service import SipService` (`SP/livekit/api/sip_service.py:59`).
- Result type: `SIPParticipantInfo` (from `livekit.protocol.sip`, re-exported by `livekit.api`).

**Service method signature** (`SP/livekit/api/sip_service.py:780-787`):

```python
async def create_sip_participant(
    self,
    create: CreateSIPParticipantRequest,
    *,
    timeout: Optional[float] = None,
    trunk_id: Optional[str] = None,
    outbound_trunk_config: Optional[SIPOutboundConfig] = None,
) -> SIPParticipantInfo
```

**Request constructor signature** (`SP/livekit/protocol/sip.pyi:924`, all keyword-optional):

```python
CreateSIPParticipantRequest(
    sip_trunk_id: str, trunk: SIPOutboundConfig, sip_request_uri: SIPRequestDest,
    sip_to_header: SIPNamedDest, sip_from_header: SIPNamedDest, sip_call_to: str,
    to_user_override: str, sip_number: str, room_name: str, participant_identity: str,
    participant_name: str, participant_metadata: str, participant_attributes: Mapping[str, str],
    dtmf: str, play_ringtone: bool, play_dialtone: bool, hide_phone_number: bool,
    headers: Mapping[str, str], include_headers: SIPHeaderOptions,
    ringing_timeout: Duration, max_call_duration: Duration, krisp_enabled: bool,
    media_encryption: SIPMediaEncryption, media: SIPMediaConfig,
    wait_until_answered: bool, display_name: str, destination: Destination,
)
```

Field list confirmed by `__slots__` at `sip.pyi:855`. Minimum useful set for an outbound
dial: `sip_trunk_id`, `sip_call_to`, `room_name`, `participant_identity`.

**Error surface**: dial failures are raised as `livekit.api.SipCallError`
(`SP/livekit/api/twirp_client.py:90`, subclass of `ServerError` at line 50), converted in
`_as_sip_error` at `sip_service.py:51-56` when the server error metadata contains
`sip_status_code`. `SipCallError.sip_status_code` (`twirp_client.py:97`) exposes the SIP
response code, e.g. 486 Busy Here, and `.sip_status` (line 108) the reason text.

---

## 2. Waiting until the call is answered

**Yes.** The field is `wait_until_answered: bool` on `CreateSIPParticipantRequest`
(`SP/livekit/protocol/sip.pyi:921`, in `__slots__` at line 855, in the constructor at line 924).

Client-side behavior when it is set (`SP/livekit/api/sip_service.py:802-811`):

- The client pins `ringing_timeout` to 30 s if you did not set it
  (`_pin_ringing_timeout`, `SP/livekit/api/_dial_timeout.py:26-34`, `DEFAULT_RINGING_TIMEOUT = 30.0` at line 16).
- The HTTP request timeout is raised to at least `ringing_timeout + 2 s`
  (`dial_timeout`, `_dial_timeout.py:37-51`, `RINGING_TIMEOUT_MARGIN = 2.0` at line 22)
  so the request outlasts ringing. A longer user `timeout=` is honored.
- The related knob `ringing_timeout: Duration` is at `sip.pyi:916`.

The call returns `SIPParticipantInfo` only after answer, or raises `SipCallError` on a SIP
failure (busy, no answer, rejected).

---

## 3. Answering-machine detection (AMD)

**Yes. AMD is available in livekit-agents 1.8.1.** It is a first-class module, not a plugin.

**Import path**

```python
from livekit.agents import AMD, AMDCategory, AMDPredictionEvent
```

- Exported at `SP/livekit/agents/__init__.py:117-121` and listed in `__all__` at lines 280-282.
- Package: `SP/livekit/agents/voice/amd/__init__.py:1-4`.
- Detector class `AMD`: `SP/livekit/agents/voice/amd/detector.py:91`.
- `AMDCategory` enum: `SP/livekit/agents/voice/amd/classifier.py:28-33` with values
  `human`, `machine-ivr`, `machine-vm`, `machine-unavailable`, `uncertain`.
- `AMDPredictionEvent` (pydantic model): `classifier.py:36-56`. Fields: `type`,
  `speech_duration: float`, `category: AMDCategory`, `reason: str`, `transcript: str`,
  `delay: float`. Properties `is_human` (line 44) and `is_machine` (line 49; true for all
  three machine categories).

**Constructor** (`detector.py:169-181`, verified with `inspect.signature`):

```python
AMD(
    session: AgentSession,
    *,
    llm: NotGivenOr[LLM | LLMModels | str | None] = NOT_GIVEN,
    stt: NotGivenOr[STT | str | None] = NOT_GIVEN,
    interrupt_on_machine: bool = True,
    ivr_detection: bool = True,
    participant_identity: NotGivenOr[str] = NOT_GIVEN,
    suppress_compatibility_warning: bool = False,
    detection_options: NotGivenOr[DetectionOptions] = NOT_GIVEN,
    wait_until_finished: bool = True,
)
```

**How it is configured**

- Mechanism (docstring `detector.py:92-113`): it listens to the callee's greeting, transcribes
  it, and asks an LLM to classify it. It is not carrier-side or tone-based AMD.
- `llm` / `stt` resolution (`detector.py:184-195`): if omitted and `LIVEKIT_URL` is a
  LiveKit Cloud host with API key/secret present (`is_cloud`, `SP/livekit/agents/utils/misc.py:42-46`),
  it auto-selects `google/gemini-3.1-flash-lite` and `cartesia/ink-whisper` via LiveKit
  Inference (`_DEFAULT_LLM_MODEL` / `_DEFAULT_STT_MODEL`, lines 166-167). Otherwise it
  reuses the session's own LLM and the session's STT transcripts. Pass `None` explicitly to
  force reuse of the session's models. Pass an inference model string or an `LLM`/`STT`
  instance to override.
- Evaluated model lists that suppress the compatibility warning: `detector.py:44-63`
  (`deepgram/nova-3` is on the STT list, `openai/gpt-4.1-mini` on the LLM list).
- `detection_options` (`DetectionOptions` TypedDict, `detector.py:70-77`) overrides timing:
  `human_speech_threshold`, `human_silence_threshold`, `machine_silence_threshold`,
  `no_speech_threshold`, `timeout`, `max_endpointing_delay`, `prompt`. Defaults live in
  `classifier.py:17-22`: 2.5 s, 0.5 s, 1.5 s, 10.0 s, 20.0 s, 3.0 s. The classification
  prompt text is `AMD_PROMPT` at `classifier.py:58`.
- SIP-awareness (`detector.py:65-66`, `442-448`, `470-483`): if the audio publisher is a
  SIP participant, the timers and audio/transcript processing are deferred until the
  participant attribute `sip.callStatus == "active"`, so ringback and early media do not
  poison the classifier. This uses `wait_for_participant_attribute`
  (`SP/livekit/agents/utils/participant.py:71`).
- Lifecycle: use as an async context manager (`__aenter__` at `detector.py:299`, which calls
  `_run` at line 361). `_run` pauses speech-playout authorization on the session
  (`detector.py:393-394`) and disables session-level `ivr_detection` (lines 376-378).
  `aclose` at line 331 resumes authorization. The session also closes its AMD on shutdown
  (`SP/livekit/agents/voice/agent_session.py:1246-1248`) and exposes it as
  `AgentSession.amd` (`agent_session.py:739-741`).
- Recommended order (docstring `detector.py:105-119`): start AMD **before** creating the
  SIP participant so no greeting audio is missed:

```python
async with AMD(session, llm="openai/gpt-4.1-mini") as detector:
    await ctx.api.sip.create_sip_participant(...)   # wait_until_answered=True
    result = await detector.execute()
```

**What fires when a machine is detected**

- Awaitable result: `await detector.execute() -> AMDPredictionEvent`
  (`detector.py:243-271`). It blocks until the classifier's verdict is ready, then:
  - if `result.is_machine` and `interrupt_on_machine`: `session.interrupt(force=True)` (lines 259-260);
  - if `result.category == AMDCategory.MACHINE_IVR` and `ivr_detection`: starts IVR navigation (lines 262-265);
  - resumes speech authorization so the agent can talk to a human immediately (lines 268-269).
  - Raises `RuntimeError("amd closed before a result was available")` if closed early (line 255).
- Event callback: `AMD` is an `EventEmitter[Literal["amd_prediction"]]` (`detector.py:91`).
  It emits `"amd_prediction"` with the `AMDPredictionEvent` at `detector.py:568`, so
  `detector.on("amd_prediction", cb)` works. The same handler also tags the job with
  `lk.amd:<category>` (lines 550-563) and forwards to the session host (lines 565-566).
- There is **no** `"amd_prediction"` entry in `AgentSession`'s `EventTypes`
  (`SP/livekit/agents/voice/events.py:291-307`), so subscribe on the `AMD` object, not the session.
- If the call ends before any audio arrives, AMD settles with `uncertain` and
  `reason="participant_missing"` (`detector.py:461-468`, `419-422`, `438-440`).

**Session-level IVR detection** exists separately (`AgentSessionOptions.ivr_detection`,
`agent_session.py:304`, constructor default `False` at line 404), but AMD supersedes it.

---

## 4. Attaching metadata to a dispatched job and reading it back

**Attaching (explicit dispatch via API)**

- `AgentDispatchService.create_dispatch(req: CreateAgentDispatchRequest) -> AgentDispatch`
  (`SP/livekit/api/agent_dispatch_service.py:39-58`). Reach it as `LiveKitAPI().agent_dispatch`.
- `CreateAgentDispatchRequest` (`SP/livekit/protocol/agent_dispatch.pyi:18`, re-exported by
  `livekit.api` via `agent_dispatch import *` at `api/__init__.py:28`). Constructor at
  `agent_dispatch.pyi:39`:

```python
CreateAgentDispatchRequest(agent_name: str, room: str, metadata: str,
                           restart_policy: JobRestartPolicy, deployment: str,
                           attributes: Mapping[str, str])
```

  - `metadata: str` is at `agent_dispatch.pyi:35`. It is a free-form string, so JSON-encode
    a dict yourself. `attributes: Mapping[str, str]` is a second, key/value channel.
- Explicit dispatch requires the worker to register an `agent_name`
  (`SP/livekit/agents/worker.py:219-220` for `ServerOptions.agent_name`, and the
  `AgentServer.rtc_session(agent_name=...)` decorator at `worker.py:477-493`;
  `AgentServer` class at line 297, `WorkerOptions = ServerOptions` alias at line 285).

**Attaching (room-create or token-embedded dispatch)**

- `RoomAgentDispatch(agent_name, metadata, restart_policy, deployment, attributes)`
  (`agent_dispatch.pyi:41`, ctor line 60, `metadata` at line 56).
- Put it in `CreateRoomRequest.agents` (`SP/livekit/protocol/room.pyi:48`) or in
  `RoomConfiguration.agents` (`room.pyi:233`, ctor line 235) and attach that to an access
  token with `AccessToken.with_room_config(...)` (`SP/livekit/api/access_token.py:193-195`;
  serialized as the `roomConfig` claim at lines 118-119).

**Reading it back in the agent**

- `JobContext.job -> livekit.protocol.agent.Job` (`SP/livekit/agents/job.py:454-456`).
- `Job.metadata: str` is at `SP/livekit/protocol/agent.pyi:66`; `Job.attributes: ScalarMap[str, str]`
  at line 71; `Job.dispatch_id` and the full slot list at line 39.
- So inside the entrypoint: `ctx.job.metadata` (string; `json.loads` it if you sent JSON) and
  `ctx.job.attributes`. The framework itself reads `job.attributes` this way at `job.py:501`.
- `JobProcess.job` (`job.py:989-990`) exposes the same proto in the prewarm process.

---

## 5. Subscribing to live user transcript events during a session

**API**

- `AgentSession` subclasses `rtc.EventEmitter[EventTypes]`
  (`SP/livekit/agents/voice/agent_session.py:362`).
- `AgentSession.on(event: EventTypes, callback: Callable | None = None) -> Callable`
  (`SP/livekit/rtc/event_emitter.py:120-132`; usable as a decorator when `callback` is omitted).
- Event name: `"user_input_transcribed"`, listed in `EventTypes`
  (`SP/livekit/agents/voice/events.py:291-307`, entry at line 294).
- Payload: `UserInputTranscribedEvent` (`events.py:327-335`) with fields
  `transcript: str`, `is_final: bool`, `item_id: str | None`, `speaker_id: str | None`,
  `language: LanguageCode | None`, `created_at: float`.

```python
from livekit.agents.voice.events import UserInputTranscribedEvent

@session.on("user_input_transcribed")
def _on_transcript(ev: UserInputTranscribedEvent):
    ...  # ev.transcript, ev.is_final
```

**Where it is emitted**

- `AgentSession._user_input_transcribed` emits it at `agent_session.py:2094-2107`
  (`self.emit("user_input_transcribed", ev)` on line 2107).
- STT pipeline: interim results come from `AgentActivity.on_interim_transcript`
  (`SP/livekit/agents/voice/agent_activity.py:2346`, emit call at line 2351, `is_final=False`)
  and finals from `on_final_transcript` (line 2380, emit call at line 2385, `is_final=True`).
- Realtime-model pipeline: `_on_input_audio_transcription_completed` at
  `agent_activity.py:2094-2100`.
- `EventEmitter.on` callbacks are invoked synchronously inside `emit`, so keep them cheap
  or schedule a task.

**Related channel (not needed for in-process use)**: transcripts are also published to the
room as text streams on topic `lk.transcription`
(`TOPIC_TRANSCRIPTION = "lk.transcription"`, `SP/livekit/agents/types.py:81`, used in
`SP/livekit/agents/voice/room_io/_output.py:537`). That is for frontends, not the agent.

---

## 6. Fallback signals if AMD were unavailable

AMD **is** available (section 3), so this is advisory only. If the built-in `AMD` had to be
avoided (for example because it needs an LLM round-trip and its own STT stream), two
fallback signals that the installed SDK can already produce:

**Fallback A: greeting length and silence pattern (VAD / STT timing).**
Subscribe to `"user_input_transcribed"` and `"user_state_changed"` after
`sip.callStatus` becomes `"active"`. A human greeting is short and followed by silence
(the classifier itself uses `human_speech_threshold = 2.5 s` and
`human_silence_threshold = 0.5 s`, `classifier.py:17-18`). A voicemail greeting runs long
without yielding (`machine_silence_threshold = 1.5 s`, `no_speech_threshold = 10 s`).
Rule: if the first speech burst exceeds roughly 2.5 s with no pause, treat as machine.
- Tradeoffs: cheap, no extra model, language-agnostic. But it misclassifies talkative humans
  and terse voicemail greetings, is sensitive to carrier early media unless gated on
  `sip.callStatus == "active"`, and cannot distinguish IVR from voicemail.

**Fallback B: transcript keyword heuristics on the first final transcript.**
Match the first final `UserInputTranscribedEvent.transcript` against voicemail phrasing
("leave a message", "after the tone", "not available", "mailbox is full", "press 1").
Humans open with "hello", "hi", a name, or a question.
- Tradeoffs: catches the classic greetings well and can tell IVR from voicemail by menu
  words. But it is language and locale specific, brittle to STT errors in the first few
  words, and ambiguous for custom greetings. It also has to wait for a final transcript,
  which arrives later than the VAD signal, so combine it with A: use A for the fast decision
  and B to confirm before leaving a message.

A third, weaker signal is the beep tone that ends most voicemail greetings. Nothing in the
installed packages detects it, so it would require custom audio-frame analysis on the
subscribed track; it is not proposed as a primary fallback.
