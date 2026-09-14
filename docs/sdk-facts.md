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

---

## 7. Ending a call from inside the agent

Used by `OutboundCaller.hangup()` in `agent.py`. The order is: let in-flight
speech finish, remove the SIP participant, close the session, then shut the job
down from the session's `close` handler.

**Letting speech finish**

- From inside a function tool, use `RunContext.wait_for_playout()`
  (`SP/livekit/agents/voice/events.py:103-110`). It waits only for the words spoken
  before the tool call (`speech_handle._wait_for_generation(step_idx=...)`).
- `SpeechHandle.wait_for_playout()` (`SP/livekit/agents/voice/speech_handle.py:205`)
  waits for the whole assistant turn, tool calls included. Calling it on the handle
  that owns the running tool raises `RuntimeError` (guard at
  `speech_handle.py:216-228`), which is why the tool path must use the `RunContext`
  variant. Outside a tool, `AgentSession.current_speech`
  (`SP/livekit/agents/voice/agent_session.py:801`) plus `wait_for_playout()` is correct.
- `RunContext` is exported from `livekit.agents` (`SP/livekit/agents/__init__.py:98`).

**Removing the SIP participant**

- `RoomService.remove_participant(RoomParticipantIdentity) -> RemoveParticipantResponse`
  (`SP/livekit/api/room_service.py:190-208`). Reach it as `JobContext.api.room`
  (`JobContext.api` at `SP/livekit/agents/job.py:438`).
- `RoomParticipantIdentity(room: str, identity: str, revoke_token_ts: int)`
  (`SP/livekit/protocol/room.pyi:101-109`, re-exported by `livekit.api`).
- Errors are `livekit.api.TwirpError`, an alias of `ServerError`
  (`SP/livekit/api/twirp_client.py:137`), with `.code` and `.message` properties
  (lines 64-70). Codes are on `TwirpErrorCode` (alias of `ServerErrorCode`, line 162):
  `NOT_FOUND = "not_found"` (line 146) is what a callee who already hung up produces.
- Fallback: `JobContext.delete_room()` (`job.py:680-700`) disconnects everyone; it
  swallows NOT_FOUND and logs other errors itself, and is a no-op in console mode.

**Why the session must be closed explicitly after `remove_participant`**

- `RoomIO._on_participant_disconnected` (`SP/livekit/agents/voice/room_io/room_io.py:406-428`)
  only closes the session when `close_on_disconnect` is set **and** the disconnect
  reason is in `DEFAULT_CLOSE_ON_DISCONNECT_REASONS`
  (`SP/livekit/agents/voice/room_io/types.py:17-21`): `CLIENT_INITIATED`,
  `ROOM_DELETED`, `USER_REJECTED`. An API removal is `PARTICIPANT_REMOVED`, which is
  not on that list, so the callee hanging up closes the session automatically but
  the agent hanging up does not.

**Closing the session**

- `AgentSession.shutdown(*, drain: bool = True)` (`agent_session.py:1212-1213`) is
  non-blocking: it schedules `_aclose_impl` with `CloseReason.USER_INITIATED` via
  `_close_soon` (lines 1199-1210), which is idempotent (`if self._closing_task: return`).
- `AgentSession.aclose()` (`agent_session.py:1353`) is the awaitable form but uses
  `drain=False`, which force-interrupts current speech (`_aclose_impl`, lines 1261-1268).
  With `drain=True` it calls `activity.drain()` (`agent_activity.py:1219`), which runs
  `Agent.on_exit` and then waits for the current speech to finish. From inside a
  function tool the current speech **is** the tool's own turn, so awaiting the close
  there would deadlock; schedule it with `shutdown()` and return from the tool.
- `CloseReason` values (`SP/livekit/agents/voice/events.py:569-575`): `error`,
  `job_shutdown`, `participant_disconnected`, `user_initiated`, `task_completed`.
  `CloseEvent` (lines 577-583) carries `reason` and `error`; the session emits
  `"close"` (in `EventTypes`, `events.py:305`) at `agent_session.py:1327`, before
  `room_io.aclose()`.

**Suppressing the spoken reply after the tool**

- Raise `StopResponse` (`SP/livekit/agents/llm/tool_context.py:140-148`, exported from
  `livekit.agents` at `__init__.py:56`) from the tool. The tool executor treats it as
  "no reply" (`SP/livekit/agents/voice/tool_executor.py:375`, `454-456`).

**Ending the job**

- `JobContext.shutdown(reason: str = "user requested")` (`job.py:798-799`) calls the
  worker's `_on_ctx_shutdown`, which resolves the job's shutdown future
  (`SP/livekit/agents/ipc/job_proc_lazy_main.py:294-300`). It is synchronous and safe
  to call from a session event callback. The worker then runs
  `add_shutdown_callback` hooks (`job_proc_lazy_main.py:434-437`) and disconnects.
- Without it, the job ends when the room closes, i.e. after the room's
  `departure_timeout` (`SP/livekit/protocol/room.pyi:38`) once the last non-agent
  participant is gone.

**Method-based tools on an `Agent` subclass**

- `Agent.__init__` collects `@function_tool` methods with `find_function_tools(self)`
  (`SP/livekit/agents/voice/agent.py:91`); `Agent.tools` (line 166) lists them.
- `_BaseFunctionTool.__get__` (`tool_context.py:248-258`) binds the tool to the
  instance and drops `self` from the published signature, so `agent.end_call(ctx, ...)`
  is directly callable in tests.
- The tool's docstring becomes its description and the `Args:` section documents
  each parameter for the LLM (`tool_context.py:401` builds the `Annotated` schema).

---

## 8. Mid-call cessation: transcripts, timing, structured classification, speech control

Used by `livekit_outbound.cessation` (the monitor and the LLM classifier) and by
`agent.py`.

**Final transcripts and their timestamp**

- `UserInputTranscribedEvent` (`SP/livekit/agents/voice/events.py:327-335`) has
  `transcript`, `is_final`, and `created_at: float` (a `time.time()` stamped when the
  event object is built). `created_at` is the anchor for the latency measurement:
  final transcript event to durable suppression.
- It does **not** carry a `transcript_delay` field in 1.8.1. The SDK computes that
  figure only as a debug log extra in `AudioRecognition`
  (`SP/livekit/agents/voice/audio_recognition.py:1213-1215`,
  `time.time() - self._last_speaking_time`), and `_last_speaking_time` is private
  (line 280). The public equivalent is derived from the user-state transition below.
- The end-of-utterance metrics event (`EOUMetrics`, `SP/livekit/agents/metrics/base.py:106-124`)
  carries `transcription_delay` and `end_of_utterance_delay`, but it is emitted per
  committed user turn via `metrics_collected`, after the transcript event and without
  a shared key, so it is not used to stamp the cessation record.

**End of speech (for the transcript-delay figure)**

- `UserStateChangedEvent` (`events.py:313-317`): `old_state`, `new_state`,
  `created_at`. The session emits it from `_update_user_state`
  (`SP/livekit/agents/voice/agent_session.py:2038-2084`); on the `speaking ->
  listening` transition `created_at` is `time.time()` at the transition (line 2082,
  `last_speaking_time` is only passed for the `speaking` state). The monitor records
  that instant and reports `transcript_delay_ms = transcript.created_at - it`.
- Both events are in `EventTypes` (`events.py:291-307`) and are delivered
  synchronously inside `emit` (`SP/livekit/rtc/event_emitter.py`), so the handlers
  only schedule a task (`asyncio.create_task`) and return; detection never runs on
  the audio path.

**Structured classification with the OpenAI plugin**

- Base `LLM.chat` (`SP/livekit/agents/llm/llm.py:159-168`) has no response-format
  parameter. The OpenAI plugin's `LLM.chat` (`SP/livekit/plugins/openai/llm.py:947-959`)
  adds `response_format`, accepting a pydantic model class; it is converted with
  `to_openai_response_format` (`SP/livekit/agents/llm/utils.py:296-309`) into a
  strict `json_schema` response format (`strict: True`, all fields required).
- `LLMStream.collect()` (`llm.py:510-540`) drains the stream and returns
  `CollectedResponse` (`llm.py:71-77`) whose `.text` is the JSON to validate with
  `Verdict.model_validate_json`.
- `openai.LLM(...)` constructor (`SP/livekit/plugins/openai/llm.py:92-106`):
  `model`, `temperature`, `max_completion_tokens`, `timeout`. `gpt-4.1-nano` is in
  the plugin's `ChatModels` list (`SP/livekit/plugins/openai/models.py`).
- `LLM.prewarm()` (`llm.py:170`) opens DNS/TLS ahead of the first request. The session
  prewarms its own LLM automatically; a second LLM instance (the classifier) must be
  prewarmed explicitly. Measured on this machine: first classifier call 3.2 s cold,
  1.0-1.5 s warm with a sentence-long rationale; the rationale was capped for that
  reason.
- `ChatContext.empty()` / `add_message(role=, content=)`
  (`SP/livekit/agents/llm/chat_context.py:420, 435`).

**Interrupting and speaking uninterruptibly**

- `AgentSession.interrupt(*, force=False)` (`agent_session.py:1556-1574`) interrupts
  the current speech and everything queued; raises `RuntimeError` if the session is
  not running or if the current speech disallows interruptions and `force` is False.
  `AgentActivity.interrupt` (`SP/livekit/agents/voice/agent_activity.py:1742-1790`)
  also clears the queue, so a reply already scheduled for the triggering utterance
  is dropped before the acknowledgement is spoken.
- `AgentSession.say(text, *, allow_interruptions=..., add_to_chat_ctx=True)`
  (`agent_session.py:1452-1484`) returns a `SpeechHandle`; raises `RuntimeError` when
  the session is not running or is closing. `SpeechHandle.wait_for_playout()`
  (`speech_handle.py:205`) completes when the acknowledgement has played out.

**Job shutdown callbacks (protecting an in-flight write)**

- `JobContext.add_shutdown_callback(cb)` (`SP/livekit/agents/job.py:581-598`): a
  coroutine function taking zero or one (`reason: str`) argument. The arity check
  uses `__code__.co_argcount`, so a bound method with defaulted parameters is
  miscounted as taking the reason; register a zero-argument wrapper. The worker
  awaits every callback before the process exits
  (`SP/livekit/agents/ipc/job_proc_lazy_main.py:434-437`), which is what lets
  `CessationMonitor.drain()` finish a suppression write that started just before
  teardown.

**Database from the event loop**

- The gate engine is created with `check_same_thread=False`
  (`src/livekit_outbound/gate/db.py:88-92`) so the synchronous SQLAlchemy writes can
  run under `asyncio.to_thread` without blocking turn handling.
