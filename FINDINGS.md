# Gemini Live API: a pending tool call across a dropped connection and session resumption

Round 1, 2026-10-02, 22:06 to 22:16 local time. Model `gemini-3.8-live`, google-genai 2.25.0 (websockets 16.1.1), Gemini Developer API with an API key (not Vertex / Agent Platform). Speech input, AUDIO output with output transcription, input transcription on. 17 of the 18 budgeted sessions were used (14 scenario runs, 3 probes). There was no quota or billing error.

Round 2 (a silent network loss, emulated with a freeze proxy, 22:36 to 23:10 the same evening) is in "Round 2: network freeze" below. It changes items 1, 3 and 7 of "What a client must do", and adds items 9 to 11.

## Short answer

- **The pending call survives the resume.** In all 14 runs that had a call and a successful resume, the resumed session accepted the `FunctionResponse` for the call id issued before the drop. No error came back. The model used the result ("Yes, ... booked"). The server never re-issued `book_slot` and never sent `toolCallCancellation`. There was exactly 1 commit per run, so no double booking.
- **If the client does not send that response, the model stays stuck at "in progress".** In R2 (3/3) the model answered "Did you book it?" with "I'm booking the 3 pm slot for you now", 2.9 to 3.1 s after the service had committed.
- **The immediate resume fails after a drop while the model is idle.** In 12/12 such runs, the first resume attempt (sent at once) was closed with `1011 Internal error encountered.` 472 to 704 ms after it started. A retry 1.0 s later worked in 11/11 runs. One run had no retry, so its session was lost. A single attempt made 1.6 s after the drop also worked (1/1). Dropping with a clean WebSocket close instead of an abort changed nothing (1/1). With the model speaking at the drop (R4), the immediate attempt worked in 3/3 runs.
- **Gap the user sees:** 2043 to 2294 ms from drop to `setupComplete` after an idle drop (n=12), and 537 to 609 ms after a drop during speech (n=3).
- **Handles: one per connection, never refreshed.** Each connection received exactly one `sessionResumptionUpdate`, right after `setupComplete` (`newHandle` with 36 characters, `resumable: true`), and none after that: not after model turns, tool calls or responses. The handle in hand at every drop had been issued before the user spoke. Even so, the resumed session held everything up to the drop, so the handle points to the session, not to a snapshot taken when it was issued.
- **No transparent mode on the Developer API.** The SDK refuses `transparent` in this mode. Sent on the wire anyway, the server closed the setup with `1007 Invalid JSON payload received. Unknown name "transparent" at 'setup.session_resumption': Cannot find field.` `lastConsumedClientMessageIndex` never appeared.
- **No `goAway`** arrived in the 16 sessions that got past setup (none lasted more than 23 s).

## Part 1: what the documentation says (no sessions)

The pages were read on 2026-10-02. They are paraphrased below, with the exact figures and field names. The pages are under CC BY 4.0, and only one phrase is quoted verbatim. Error strings from the server and the SDK are verbatim, because they are measurement output.

Pages:
- [GD-SM] Gemini API, *Session management with Live API*, https://ai.google.dev/gemini-api/docs/live-api/session-management (last updated 2026-09-15; `/gemini-api/docs/live-session` redirects there)
- [GD-REF] *Live API - WebSockets API reference*, https://ai.google.dev/api/live (last updated 2026-09-04)
- [GD-CAP] *Live API capabilities*, https://ai.google.dev/gemini-api/docs/live-api/capabilities (last updated 2026-09-18)
- [GD-MODEL] *Gemini 3.8 Live* model page, https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live (last updated 2026-09-15)
- [GD-TOOLS] *Tool use with Live API*, https://ai.google.dev/gemini-api/docs/live-api/tools (last updated 2026-09-15)
- [VX-SM] Google Cloud (Vertex AI is now "Gemini Enterprise Agent Platform"), *Start and manage live sessions*, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/start-manage-session (last updated 2026-10-01; the old cloud.google.com/vertex-ai URL redirects there)
- [VX-BP] *Best practices with Gemini Live API*, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/best-practices (last updated 2026-10-01)
- [VX-REF] *Gemini Live API reference*, https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/multimodal-live (last updated 2026-10-02)

### How a handle is obtained, and how long it lasts
- To enable resumption, set `sessionResumption` in the setup message. The server then sends `SessionResumptionUpdate` messages, and the client passes the last token as `SessionResumptionConfig.handle` on the next connection. The comment in the Python sample says the server sends these updates periodically [GD-SM]. Per [GD-REF], updates are sent only if `sessionResumption` was set. The reference also says handles come from `SessionResumptionUpdate.token`, but the field is named `newHandle`.
- The Gemini API says tokens stay valid for **2 hours** after the last session terminated [GD-SM].
- Vertex says a session can be resumed **within 24 hours**, and that the cached data (text, video, audio prompts, model outputs) is kept at rest for up to 24 hours. The same page also says the resumption window is finite, **typically about 10 minutes**, after which the session state is discarded [VX-SM]. The two statements on that one page do not agree.
- Per [GD-REF], `SessionResumptionUpdate` has two fields. `newHandle` is empty when `resumable` is false. `resumable` is false at points where resumption is not possible, "when the model is executing function calls or generating", and the reference says that resuming in such a state loses some data.
- Configuration cannot change while a connection is open, but it can change (except the model) when resuming [GD-REF].

### Transparent mode
- [GD-REF] has no `transparent` field in `SessionResumptionConfig`, which has only `handle`, and no `lastConsumedClientMessageIndex` in `SessionResumptionUpdate`. The Gemini API docs do not mention transparent mode anywhere.
- Vertex documents it [VX-SM]. When transparent mode is on, the server returns the index of the client message that matches the context snapshot, so the client knows which messages to send again. [VX-BP] gives the procedure: buffer the messages you send; drop those at or below `last_consumed_client_message_index`; start the index at 1 (0 means not resumable); restart it at 1 on each new connection; replay the unacknowledged messages after reconnecting.
- google-genai 2.25.0: `types.SessionResumptionConfig` has `handle` and `transparent`. `_live_converters._SessionResumptionConfig_to_mldev` raises `ValueError('transparent parameter is only supported in Gemini Enterprise Agent Platform mode, not in Gemini Developer API mode.')`. So with an API key, the SDK cannot send it.

### What state is restored
- [VX-SM]: the server restores the previous context, and resumption works by storing cached session data. [GD-SM] and [GD-REF] give no list of what is restored.
- **Neither doc set says anything about tool calls that are pending at disconnect**: whether they survive, whether their ids stay valid, or whether the server re-issues or cancels them. The only related statements are the `resumable: false` rule above and, for VAD barge-in only, that the server drops pending function calls and sends the ids of the cancelled ones [GD-CAP].

### GoAway, session and connection limits
- `GoAway.timeLeft`: the time left before the server terminates the connection as ABORTED. It is never below a model-specific minimum that is published with the rate limits [GD-REF, GD-SM].
- Vertex: `goAway` comes 60 s before the session ends, and the default maximum is 10 minutes [VX-SM].
- Without context window compression, audio-only sessions last up to 15 minutes and audio+video sessions up to 2 minutes. A connection lasts about 10 minutes, and resumption carries the session across connections [GD-SM, GD-CAP, VX-SM].
- `ContextWindowCompressionConfig` has `slidingWindow` (`targetTokens`, default trigger/2) and `triggerTokens` (default: 80 % of the context window) [GD-REF].
- [GD-MODEL] says nothing about resumption, GoAway or session limits for `gemini-3.8-live`. It says that NON_BLOCKING is the default for function calls. [GD-TOOLS] is silent on disconnects.

### SDK surface (google-genai 2.25.0, `uv run introspect.py`)
- `LiveConnectConfig.session_resumption: SessionResumptionConfig(handle, transparent)` and `LiveConnectConfig.context_window_compression: ContextWindowCompressionConfig(trigger_tokens, sliding_window)`.
- `LiveServerSessionResumptionUpdate`: `new_handle`, `resumable`, `last_consumed_client_message_index`. The docstring says the index is sent only when `transparent` is set. It also says the index is meant for short planned reconnects (after a GoAway), not for resuming much later.
- `LiveServerGoAway.time_left` (there is no `types.GoAway`).
- `AsyncLive.connect()` reads the first server frame itself, as the setup response. It exposes it as `session.setup_complete`, so the caller never sees that frame in `receive()`. The harness taps the websocket to see it.

### What the docs do not say
- How often handles are refreshed, and whether a handle issued before a turn restores the state at that point or the latest state.
- Whether a pending function call survives a resume, whether its id stays valid, and whether the server re-issues, cancels or forgets it.
- How soon after a drop a resume can succeed, and which errors a resume can return.
- Whether a resumed connection gets a new `setupComplete`, or a session id.
- What happens to model output that was being generated at the drop.
- Which of 2 hours, 24 hours and "about 10 minutes" applies, for which API.

## Part 2: setup

`resume_test.py` is one file. It reuses the session setup, the open-mic audio streaming, the JSONL logging and the summary pattern of `../gemini-live-stop-test/stop_test.py`, and the two-phase service and `canonical_slot` key normalisation of `../gemini-live-commit-guard/guard_test.py`.

- **Session.** Same system prompt and `book_slot(slot)` declaration as the earlier harnesses (behavior unset, so NON_BLOCKING by default on this model). Config: `response_modalities=[AUDIO]`, output and input audio transcription, and `session_resumption=SessionResumptionConfig(handle=None)` from the first connection. `transparent` is not set (see Part 1). Probe P1 adds it on the wire only.
- **Speech.** `assets/audio/book.wav` ("Book me the 3pm slot tomorrow, please.", 2.686 s), `bring.wav` ("While you do that, what should I bring to the appointment?", 2.870 s, R4), `hello.wav` ("Hi, can you hear me?", 1.527 s, R0) and `did_you_book.wav` ("Did you book it?", 0.746 s). All were made with macOS `say -v Samantha --file-format=WAVE --data-format=LEI16@16000`, and all are 16 kHz 16-bit mono. They are streamed with `send_realtime_input(audio=Blob(..., "audio/pcm;rate=16000"))`, 100 ms per call, paced in real time, with silence in between. Automatic VAD stays at the server default.
- **Raw tap.** `google.genai.live.ws_connect` is replaced by a wrapper, so every frame is logged both ways, including the setup response that the SDK consumes. Each `sessionResumptionUpdate` is logged from the raw frame with its keys, `resumable`, whether a handle is present, `lastConsumedClientMessageIndex`, and a client message counter. The handle itself is never written, only its length and a 10-character SHA-256 prefix.
- **Fake service, no guard.** The `book_slot` call starts a job: prepare for `--latency` s (4.0 s; 7.0 s in R4), then commit at once. The answer is `{"status": "booked", "slot": ..., "confirmation_id": "BK-1001"}`. Any call issued after the resume would have been executed and answered too, which is how a double booking would show up. None was issued.
- **Disconnect.** First the harness cancels its own mic and receive tasks, so nothing more is sent or read. Then it calls `transport.abort()` on the websockets `ClientConnection` under the SDK session. The TLS/TCP transport is torn down at once, with no WebSocket close frame and no TLS close_notify; locally the close code is 1006. Then the SDK `connect()` context exits. In the offline dry run, a local websockets server saw "no close frame received or sent". P2 uses a clean close (code 1000) at the same point.
- **Resume.** Same config, `handle` = the last `newHandle` that came with `resumable: true`. The first attempt goes at once. On failure, retries follow 1, 2, 4 and 8 s later with the same handle, all within the same run. The first live run (R1 run 1) happened before this retry logic existed. The pre-disconnect call's response is held until a resumed connection is up, then sent on it (R2: never sent).
- **Scenarios**, run with N=3 unless noted:
  - R1: drop 1.0 s after the `toolCall` (call pending, before the commit). After the resume, send the old response when the job commits; ask once the reaction has settled (at least 3 s after the response, model idle, 1 s of quiet).
  - R2: same drop point. The old response is never sent; ask 2.0 s after the resume.
  - R3: drop 0.5 s after the commit, before the response. After the resume, send the old response at once, then ask (same settling rule as R1).
  - R4: `bring.wav` starts 0.5 s after the call; the drop comes 1.0 s after the first model audio chunk that follows the call (model speaking, call pending, latency 7.0 s as in stop-test G2). After the resume, send the old response when the job commits, then ask.
  - R0 (control, N=1): "Hi, can you hear me?", model reply, wait for an update (none came), then drop 1.0 s later; ask 2 s after the resume. No tool call.
  - P1 (N=1): R1 with `"transparent": true` written into `setup.sessionResumption` on the wire.
  - P2 (N=1): R1 with a clean WebSocket close instead of the abort.
  - P3 (N=1): R1 with the first resume attempt 1.6 s after the drop.
- **End of run.** At least 4 s after the end of the ask clip; the model has answered and is idle; all jobs are done; 3 s of quiet. The run also ends at most 15 s after the ask. Runs are 3 s apart.
- **Consistent?** This column compares the model's answer to "Did you book it?" with the service's commit count. Each answer was checked by hand; the automatic keyword classification in `summary.md` agrees on all runs.

Before any live session, the harness was dry-run against a local fake Live server (ws://127.0.0.1, dummy key) for R0 to R4, including a failed first resume attempt.

Reproduce:
```sh
uv sync && uv run introspect.py
uv run resume_test.py --scenario R1 -n 1                       # R1 run 1 (before retries existed)
uv run resume_test.py --scenario R0 -n 1
uv run resume_test.py --scenario R1 -n 3 --first-run 2
uv run resume_test.py --scenario R2 -n 3
uv run resume_test.py --scenario R3 -n 3
uv run resume_test.py --scenario R4 -n 3
uv run resume_test.py --scenario R1 -n 1 --inject-transparent --name P1_R1_transparent_probe
uv run resume_test.py --scenario R1 -n 1 --drop-mode clean --name P2_R1_clean_close
uv run resume_test.py --scenario R1 -n 1 --resume-delay 1.6 --name P3_R1_resume_delay1.6
```
Raw events are in `results/<name>.jsonl`, the automatic tables in `results/summary.md`, and one line per session in `results/sessions.log`.

## Results

Times are in ms since the session started. "Handle" means the only `newHandle` the run had received before the drop; the offset is relative to the `toolCall`. "Resume" is the time from the drop to the `setupComplete` of the connection that came up, with the failed attempts in brackets. "+N ms" after the response is when the first model transcript arrived after it.

### Facts common to all runs
- **Updates.** There was exactly one `sessionResumptionUpdate` per connection. It came right after `setupComplete`: 399 to 606 ms after the start on the first connection, and within 2 ms of `setupComplete` on resumed connections. Its keys were always `newHandle` and `resumable` (true). None arrived later in any run, whether after a model turn, a `toolCall`, a tool response, or the 4.8 s between the end of the R0 reply turn and the drop. No update with `resumable: false` was ever seen. `lastConsumedClientMessageIndex` never appeared.
- **Setup on resume.** Every resumed connection (15/15) started with a new `setupComplete`. Its payload was `{}`: no `sessionId`, on any connection.
- **What was restored.** The handle predated the request every time, yet the resumed session knew the request and the pending call (R1 to R4, P2, P3). It also knew nothing had been booked (R0). Server-side VAD `audioOffset` values on the resumed connection kept counting from the session start. For example, R0: 13.16 s at the ask, against about 13.0 s of audio streamed over both connections. This fits one continuous session.
- **No `goAway`, no `toolCallCancellation`, no server `error` frame** in any session.
- **Failed resume attempts:** 12, every one on a drop while the model was idle. Each was a close with code 1011, reason `Internal error encountered.`, 472 to 704 ms after the attempt started, before any server frame arrived.

### R0, control (no tool call)
| run | handle | resume | answer to "Did you book it?" | commits | consistent |
|---|---|---|---|---|---|
| 1 | resumable, @439 (before the user spoke) | 2285 (1 failed: 1011 after 704) | "I haven't booked anything yet. Please tell me the slot you would like to book." | 0 | yes |

### R1, drop 1.0 s after the call, old response sent after resume
| run | handle resumable at disconnect | resume | re-issued call | old response | answer to "Did you book it?" | commits | consistent |
|---|---|---|---|---|---|---|---|
| 1 | yes, @470 (-3625 vs call) | failed: 1011 after 617, no retry in the harness yet | n/a | not sent (no connection) | n/a | 0 (job aborted at run end) | n/a |
| 2 | yes, @441 (-3622) | 2097 (1 failed, 552) | no | accepted; +715 "I'm booking the 3:00 PM slot for you tomorrow." | "Yes, the 3:00 PM slot for tomorrow is all booked." | 1 | yes |
| 3 | yes, @426 (-3617) | 2294 (1 failed, 623) | no | accepted; +652 "I'm booking the 3 p.m. slot for you tomorrow." | "Yes, your 3 p.m. slot tomorrow is successfully booked." | 1 | yes |
| 4 | yes, @425 (-3620) | 2134 (1 failed, 598) | no | accepted; +682 "I'm booking the 3 p.m. slot for tomorrow for you." | "Yes, your 3 p.m. slot for tomorrow is officially booked." | 1 | yes |

### R2, same drop, old response never sent, ask 2 s after resume
| run | handle resumable at disconnect | resume | re-issued call | old response | answer to "Did you book it?" | commits | consistent |
|---|---|---|---|---|---|---|---|
| 1 | yes, @473 (-3626) | 2178 (1 failed, 598) | no | not sent | "I'm booking the 3 pm slot for you now." | 1 (committed @8102, answer @11071) | no: says in progress |
| 2 | yes, @606 (-3536) | 2281 (1 failed, 591) | no | not sent | "I am booking the 3 p.m. slot for tomorrow right now." | 1 (@8143, answer @11216) | no: says in progress |
| 3 | yes, @441 (-3613) | 2077 (1 failed, 580) | no | not sent | "I'm currently booking the three p.m. slot for you now." | 1 (@8056, answer @10923) | no: says in progress |

### R3, drop 0.5 s after the commit, before the response; old response sent after resume
| run | handle resumable at disconnect | resume | re-issued call | old response | answer to "Did you book it?" | commits | consistent |
|---|---|---|---|---|---|---|---|
| 1 | yes, @446 (-3667) | 2241 (1 failed, 668) | no | accepted; +686 "I am booking the 3 p.m. slot for tomorrow for you." | "Yes, the 3 p.m. slot tomorrow is now booked for you." | 1 | yes |
| 2 | yes, @419 (-3609) | 2117 (1 failed, 534) | no | accepted; +599 "I am booking the 3 pm slot for you tomorrow." | "Yes, your 3 pm slot for tomorrow has been successfully booked." | 1 | yes |
| 3 | yes, @502 (-3610) | 2043 (1 failed, 472) | no | accepted; +594 "I'm booking the 3 PM slot for tomorrow for you." | "Yes, the booking for tomorrow at 3 PM is confirmed." | 1 | yes |

### R4, drop while the model speaks (call pending), old response sent after resume
The drops came 5718, 5759 and 5634 ms after the call. The commit came 1.24 to 1.37 s after the drop, so after the resume, and the response went out within 4 ms of it. The handle used was again the first one (@446, @430, @399; 3.6 to 3.8 s before the call).

| run | said before the drop (cut) | resume | re-issued call | old response | answer to "Did you book it?" | commits | consistent |
|---|---|---|---|---|---|---|---|
| 1 | "I am booking that slot for you right now. Please bring any relevant documents" | 609 (first attempt) | no | accepted; +636 "It's all booked! Just bring your identification and any relevant documents." | "Yes, your appointment is fully booked for tomorrow at 3 PM." | 1 | yes |
| 2 | "I am booking the 3 PM slot for you tomorrow. Please remember to bring your ID and any relevant" | 597 (first attempt) | no | accepted; +754 "please be sure to bring any relevant identification or paperwork with you." | "Yes, I have successfully booked the 3 PM slot for you." | 1 | yes |
| 3 | "I am booking the 3:00 PM slot for tomorrow. Please bring your identification and any relevant" | 537 (first attempt) | no | accepted; +741 "please make sure to bring your identification and any relevant paperwork." | "Yes, I have successfully booked your 3:00 PM slot for tomorrow." | 1 | yes |

### Probes (R1 drop point)
| probe | result |
|---|---|
| P1, `"transparent": true` on the wire | Setup refused 44 ms after it was sent: close `1007`, `Invalid JSON payload received. Unknown name "transparent" at 'setup.session_resumption': Cannot find field.` |
| P2, clean close (code 1000) instead of abort | Same as an abort: the immediate attempt got 1011 after 558 ms; resumed 2137 ms after the close; old response accepted (+647 "I am booking the 3 PM slot for tomorrow for you."); answer "Yes, the 3 PM slot for tomorrow has been successfully booked."; 1 commit; consistent |
| P3, single first attempt 1.6 s after the drop | Worked at the first try; resumed 2221 ms after the drop; +673 "I'm booking the 3 PM slot for you tomorrow."; answer "Yes, it's all booked and confirmed for you."; 1 commit; consistent |

Totals: 14 runs had a call and a successful resume (R1 x3, R2 x3, R3 x3, R4 x3, P2, P3). There were 0 re-issued calls, 0 cancellations, 0 errors on the old response, and 1 commit each. The answer matched the service in 11/11 runs where the old response was sent, and in 0/3 where it was not (R2). R3 run 1 and R4 run 1 had the call's args as `tomorrow at 3pm` instead of `tomorrow 3pm`; the normalised key is the same (`tomorrow 15:00`). It made no difference, since nothing was re-issued.

How the model reacted to the old response, compared with no disconnect: its first sentence came 594 to 754 ms after the response (n=11). In 8 of the 11 (every idle-drop run) it was the announcement the system prompt asks for after calling ("I'm booking..."). In R4 it was "It's all booked!" once, and twice a redo of the sentence the drop had cut. The stop-test audio_D runs (2026-09-29, same model and prompt, no disconnect, response 4 s after the call) show the same "I'm booking..." sentence 519 to 659 ms after the response (n=3). So the resume does not visibly change this reaction.

## Transcripts (verbatim, from the JSONL)

R1 run 2, drop with a pending call, response after resume:
```
4061  input   "Put me the 3:00 p.m. slot tomorrow, please."
4063  toolCall book_slot {"slot": "tomorrow 3pm"} id=call_465566        (turn_complete at once)
5064  drop: transport.abort()        last update @441 resumable=true (the only one)
5065  resume attempt 1 (same handle) -> 5617 close 1011 "Internal error encountered."
6619  resume attempt 2 -> 7161 setupComplete {} ; 7163 sessionResumptionUpdate (new handle, resumable)
8064  service commits BK-1001
8068  toolResponse for call_465566 on the new connection
8783  model   "I'm booking the 3:00 PM slot for you tomorrow."
11106 user    "Did you book it?"
12779 model   "Yes, the 3:00 PM slot for tomorrow is all booked."
```

R2 run 1, same drop, no response sent:
```
4099  toolCall book_slot id=call_677571 ; 5101 drop ; 5101 attempt 1 -> 1011 after 598 ms
7279  setupComplete on attempt 2 (2178 ms after the drop)
8102  service commits BK-1001 (response withheld)
9282  user    "Did you book it?"
11071 model   "I'm booking the 3 pm slot for you now."
```

R4 run 2, drop while the model speaks:
```
4046  toolCall book_slot id=call_501864 ; 4548 user "While you do that, what should I bring to the appointment?"
8804  model   "I am booking the 3 PM slot for you tomorrow. Please remember to bring your ID and any relevant"
9806  drop (model mid-sentence) ; resume attempt 1 -> 10403 setupComplete (597 ms)
11048 service commits BK-1001 ; 11049 toolResponse for call_501864 on the new connection
11803 model   "please be sure to bring any relevant identification or paperwork with you."
14055 user    "Did you book it?"
15760 model   "Yes, I have successfully booked the 3 PM slot for you."
```

R0, control without a call:
```
3105  input   "Hi, can you hear me?"
3106  model   "Yes, I can hear you clearly. How can I help you today?"   (turn_complete 6298, no update after it)
11141 drop ; attempt 1 -> 1011 after 704 ms ; attempt 2 -> 13426 setupComplete (2285 ms)
15428 user    "Did you book it?"
19021 model   "I haven't booked anything yet. Please tell me the slot you would like to book."
```

P1, transparent probe:
```
61    setup sent with sessionResumption {"transparent": true}
105   close 1007 "Invalid JSON payload received. Unknown name "transparent" at 'setup.session_resumption': Cannot find field."
```

## Round 2: network freeze

Round 2, 2026-10-02, 22:36 to 23:07 local time; same model, SDK and harness. It used 9 of the 12 budgeted sessions (N1 x3, N2 x3, N3 x2, probe P4 x1). There was no quota or billing error.

### Short answer

- **Resuming is impossible while the old connection is silently alive.** When packets stop but neither side sees a close, every resume attempt with the handle is refused with `1011 Internal error encountered.`, 495 to 1568 ms after it starts (median 575 ms). That held for 40 of 40 attempts, from 1.8 s to 133 s after the loss (N1, N2), and in probe P4 nothing worked for 12.8 minutes.
- **If the old path never returns, the session is lost (P4, N=1).** Resume attempts went through three phases: refused (up to +184 s), then hanging with no answer (+244 to +454 s), then refused again. At +489 s the server closed the frozen connection with `1011 Internal error encountered.`, sending only a close frame while TCP stayed up. Every resume after that was refused, including after the client had received the close. The booking committed 3 s after the loss was never reported to the user.
- **During the loss the server sent nothing:** no WebSocket ping, no close, 0 bytes in 30 to 145 s of freeze, and its TCP connection stayed ESTABLISHED. It answers client pings (median RTT 10.4 to 25 ms), but it never probed the client itself.
- **When the path comes back, the old connection is intact.** After 30 s (N3), the server accepted the response for the pending call on that same connection, and the model answered "Did you book it?" correctly. After 46 to 145 s (N1, N2), the old connection was still OPEN and sent no data frames in 8 s.
- **Closing the old connection unblocks the resume at once, at least within 145 s of the loss.** Once the client could close it (close 1000, echoed by the server with 1000 in 10 to 86 ms), the resume worked on the first attempt, 559 to 600 ms later (5/5). The pending call was still there up to 153 s after the loss: the response for the old id was accepted, and the answer matched the service (5/5).
- **No `toolCallCancellation`, no re-issued call, no `goAway`, and 1 commit per run.**

### Method

- **Freeze proxy.** `freeze_proxy.py` is an asyncio HTTP CONNECT proxy on 127.0.0.1. It accepts only `generativelanguage.googleapis.com:443`, and it copies the TLS bytes untouched, so it never sees plaintext or the key.
  - `freeze(tunnel)` stops forwarding in both directions without closing either socket: both transports call `pause_reading()` and nothing is written. `unfreeze()` resumes, and the data held in buffers is delivered.
  - While a tunnel is frozen, it reads the macOS `TCP_CONNECTION_INFO` of both sockets every 250 ms without consuming any data: TCP state, bytes received by the kernel, bytes queued to send. A FIN or RST from the server during the freeze would therefore be timestamped.
- **Routing.** google-genai 2.25.0 calls `websockets.asyncio.client.connect` without a `proxy` argument. websockets 16 then defaults to `proxy=True`, which reads the system or environment proxy, so `HTTPS_PROXY` would also work. The harness passes `proxy="http://127.0.0.1:<port>"` explicitly through its existing `ws_connect` wrapper, and maps each SDK connection to its tunnel by the local port.
- **Keepalive.** websockets' keepalive (ping every 20 s, 20 s timeout, which the SDK does not override) is switched off with `ping_interval=None`. Otherwise the client library itself would close a frozen socket after 20 to 40 s.
- **Offline check** (`uv run test_freeze_proxy.py`, local websockets server, no key). During a 5 s freeze the client got 0 messages, saw no close, and stayed OPEN; the 20 messages it sent did not reach the server. After unfreeze the backlog arrived both ways. When the server closed during a freeze, the probe saw `ESTABLISHED -> CLOSE_WAIT` 2.26 s later, while the client still showed OPEN; the client saw code 4001 only after unfreeze.
- **Client loss rule.** A WebSocket ping goes out every 0.5 s. The link is declared lost when neither a server frame nor a pong has arrived for 2.0 s.
  - The pings matter. With the model idle, a healthy connection carries no server frames for many seconds (round 1), so a frame-only rule would misfire.
  - Detection came 1806 to 1895 ms after the freeze (n=9).
  - On detection, the client stops reading and writing on the old connection but leaves it open: mic, receiver and pinger tasks are cancelled; the socket is not closed.
- **Scenarios.** All use the R1 drop point (freeze 1.0 s after the `toolCall`, call pending, model idle, commit 4 s after the call) and the same speech clips.
  - **N1:** resume at detection, with retries while the old tunnel stays frozen. Run 1 retried after 1, 2, 4, 8 and 16 s; runs 2 and 3 added 32 and 64 s, reaching 133 s after the freeze. The harness unfreezes 10 s after the last attempt. Because no attempt succeeded while frozen, the "new session affected by the unfreeze" check had no new session to look at.
    - Runs 2 and 3 then watch the old connection for 8 s, close it cleanly (`close(1000)`, which now reaches the server), resume as in round 1, send the response for the pending call, and ask "Did you book it?".
    - Run 1 predates that step: the harness only watched the old connection for 8 s and then ended the run.
  - **N2:** same as N1, with the first attempt 5 s after the freeze and retries after 1, 2, 4, 8 and 16 s.
  - **N3:** freeze, no resume, the old socket kept open. Unfreeze after 30 s, read the old connection for 3 s, then use it again: send the pending call's response, then ask.
  - **P4 (probe, N=1):** N1 with one resume attempt every 60 s while frozen, for up to 12 minutes.
- **Faithfulness.** This is an application-level freeze, not a perfect packet loss. The proxy's kernel still ACKs what fits in its receive buffer, and answers zero-window probes and TCP keepalives. In a real loss, nothing comes back, so a sender with data in flight would eventually get a TCP timeout or reset. Here the server had nothing to send while idle, so in this state the two cases look the same to it, unless it uses TCP keepalive (it would then get answers here and none in a real loss).

Reproduce:
```sh
uv run test_freeze_proxy.py                                        # offline
uv run resume_test.py --scenario N1 -n 1 --budget 29                # N1 run 1 (no recovery step yet)
uv run resume_test.py --scenario N1 -n 2 --first-run 2 --reconnect-retries 7 --budget 29
uv run resume_test.py --scenario N2 -n 3 --budget 29
uv run resume_test.py --scenario N3 -n 2 --budget 29
uv run resume_test.py --scenario N1 -n 1 --name P4_N1_long_freeze --retry-fixed 60 --reconnect-retries 12 --budget 29
```

### N1, resume at detection, old tunnel frozen and open

In the table: "Freeze" is the freeze time, counted from the call. "Old conn during freeze" covers what the server sent and the state of the proxy-side TCP socket facing the server. "Recovery" is the client closing the old connection once the path is back, then resuming.

| run | freeze | detected | resume attempts while frozen | old conn during freeze | unfreeze | old conn after unfreeze | recovery | old response | answer to "Did you book it?" | commits | consistent |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | +1001 | +1806 | 6, at +1.8 to +35.7 s, all `1011` (555 to 620 ms each) | 0 B, ESTABLISHED, no close | +46.3 s | still OPEN, 0 data frames in 8 s | not in this harness version; the session was never resumed | never sent (booking committed @8174, user never told) | n/a | 1 | n/a |
| 2 | +1002 | +1890 | 8, at +1.9 to +133.0 s, all `1011` | 0 B, ESTABLISHED, no close | +144.6 s | still OPEN, 0 data frames in 8 s; client close 1000, server echo 1000 in 10 ms | resumed 600 ms after the close, first attempt | accepted; +715 "I am booking the 3 PM slot for you tomorrow." | "Yes, the booking for tomorrow at 3 PM is confirmed." | 1 | yes |
| 3 | +1001 | +1812 | 8, at +1.8 to +133.0 s, all `1011` | 0 B, ESTABLISHED, no close | +143.5 s | still OPEN, 0 data frames in 8 s; close 1000/1000 in 66 ms | resumed 596 ms after the close, first attempt | accepted; +631 "I'm booking the 3 PM slot for you tomorrow." | "Yes, the 3 PM slot for tomorrow is successfully booked." | 1 | yes |

### N2, first resume attempt 5 s after the freeze

| run | freeze | detected | resume attempts while frozen | old conn during freeze | unfreeze | old conn after unfreeze | recovery | old response | answer to "Did you book it?" | commits | consistent |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | +1002 | +1895 | 6, at +5.0 to +38.9 s, all `1011` | 0 B, ESTABLISHED | +49.5 s | OPEN, 0 data frames; close 1000/1000 in 18 ms | resumed 559 ms after the close, first attempt | accepted; +641 "I am booking the 3:00 p.m. slot for you tomorrow." | "Yes, your 3 p.m. slot for tomorrow is booked." | 1 | yes |
| 2 | +1001 | +1891 | 6, at +5.0 to +38.8 s, all `1011` | 0 B, ESTABLISHED | +49.4 s | OPEN, 0 data frames; close 1000/1000 in 14 ms | 568 ms, first attempt | accepted; +776 "I am booking the 3 pm slot for you tomorrow." | "Yes, the booking for the tomorrow 3 PM slot is confirmed." | 1 | yes |
| 3 | +1001 | +1883 | 6, at +5.0 to +38.8 s, all `1011` | 0 B, ESTABLISHED | +49.3 s | OPEN, 0 data frames; close 1000/1000 in 86 ms | 566 ms, first attempt | accepted; +658 "I'm booking that 3 p.m. slot for you now." | "Yes, it is successfully booked." | 1 | yes |

### N3, never resumed; unfreeze after 30 s and reuse the old connection

| run | freeze | detected | server during the 30 s freeze | old conn in the 3 s after unfreeze (client silent) | reuse of the old connection | old response | answer to "Did you book it?" | commits | consistent |
|---|---|---|---|---|---|---|---|---|---|
| 1 | +1002 | +1890 | 0 B, ESTABLISHED, no close | OPEN, 0 frames | works | accepted; +717 "I'm booking the 3 p.m. slot for you for tomorrow." | "Yes, the 3 p.m. slot for tomorrow has been successfully booked." | 1 | yes |
| 2 | +1002 | +1868 | 0 B, ESTABLISHED, no close | OPEN, 0 frames | works | accepted; +814 "I'm booking the 3 PM slot for you tomorrow." | "Yes, the 3 PM slot for tomorrow is confirmed and booked." | 1 | yes |

### P4, how long the lockout lasts if the old path never comes back

One resume attempt every 60 s while the old tunnel stayed frozen, then unfreeze at +777 s, then the round-1 resume schedule. The call was issued at 4105 ms, the freeze came at 5106 ms, and the service committed at 8107 ms (+3.0 s).

| time after freeze | what happened |
|---|---|
| +1.8 s | the client rule fires (no frame and no pong for 2030 ms); attempt 1 starts at once |
| +1.8 s to +183.6 s | attempts 1 to 4 (starts): each refused with `1011` after 495 to 686 ms |
| +244.1 s to +454.2 s | attempts 5 to 8: each hung, with no `setupComplete`, no frame and no close within 10 s (the harness gave up on each) |
| +489 s | the server sends 53 bytes on the frozen connection (probe: kernel `rxbytes` 8461 -> 8514); TCP stays ESTABLISHED, with no FIN or RST up to +777 s |
| +524.2 s to +766.6 s | attempts 9 to 13: each refused with `1011` after 566 to 697 ms |
| +777 s | unfreeze: 22 ms later the client receives those 53 bytes as a close frame, code `1011`, reason `Internal error encountered.`, with no data frame before it |
| +785.3 s to +802.9 s | 5 more attempts, now that the old connection is closed: each refused with `1011` after 607 to 693 ms |

Outcome: in 13.4 minutes the session was never resumed, and the handle stopped working altogether. The response for the pending call could not be delivered, and the committed booking was never reported. No `goAway` arrived (none would have been visible while frozen, and none was queued). Why the attempts hung for a while, and what the server's close at +489 s (about 8.2 minutes after the session started) corresponds to, is not documented; with N=1 these are observations, not limits to design against.

### Server close codes and traffic, round 2

- **Resume attempts while the old connection was open:** 54 (40 in N1 and N2; 14 in P4, of which 5 came after the server had closed the old connection) refusals, each a close with `1011`, reason `Internal error encountered.`, 495 to 1568 ms after the attempt started (median 575 ms; only one above 735 ms) and before any server frame.
- **Old connection during the freeze:** no close frame, no FIN or RST (the probe saw ESTABLISHED throughout), and 0 bytes from the server, including no WebSocket pings, in every run up to 145 s. In P4 the server sent one frame at +489 s, the close `1011 Internal error encountered.`, still without FIN or RST; it reached the client only at unfreeze. P4 also had 4 attempts (+244 to +454 s) that got no answer at all within 10 s.
- **Old connection after unfreeze:**
  - The client's frames queued during the 1.8 to 1.9 s before detection (mic audio and pings, about 80 KB) reached the server at unfreeze.
  - The server sent back about 138 bytes, consistent with pongs for the queued pings, and no data frames.
  - The connection stayed OPEN until the client closed it. The client's close (1000) was echoed with 1000 and an empty reason.
- **N3 after reuse:** normal traffic (`serverContent` with audio and transcripts, `voiceActivity`, `turnComplete`, `usageMetadata`). The server also sent frames that were `{}` or `{"serverContent": {}}`, interleaved with the audio chunks. Every frame was logged only after the unfreeze, so we cannot say whether these empty frames are normal.
- **Audio clock.** In every N run, server-side VAD `audioOffset` was 10.04 s at the "Did you book it?" clip. That is the audio actually received (before the freeze, the queued audio, then the new audio), not the wall time: the freeze gap does not count.
- **Absent:** no `goAway`, no `toolCallCancellation`, no re-issued call, and no server `error` frame in any round-2 run.

### Transcripts, round 2 (verbatim, from the JSONL)

N1 run 2, silent loss, locked out until the client could close the old connection:
```
4139   toolCall book_slot {"slot": "tomorrow 3pm"} id=call_654640        (turn_complete at once)
5141   freeze (old tunnel: no bytes either way, sockets open)
7031   client rule fires: no frame and no pong for 2031 ms -> old connection left open, unused
7031   resume #1 (handle from @~0.5 s) -> close 1011 "Internal error encountered." after 579 ms
...    resume #2 to #8 at +3.5, +6.1, +10.6, +19.2, +35.8, +68.4, +133.0 s -> 1011 each time
8141   service commits BK-1001 (response held: no session)
149759 unfreeze (+144.6 s): old connection still OPEN; 0 data frames in the next 8 s
157761 client closes the old connection (1000); server echoes 1000 at 157771
158371 resume #9 -> setupComplete {} on the first try (600 ms after the close)
158377 toolResponse for call_654640 (issued 154 s earlier) on the new connection
159092 model   "I am booking the 3 PM slot for you tomorrow."
161379 user    "Did you book it?"
162874 model   "Yes, the booking for tomorrow at 3 PM is confirmed."
```

N3 run 1, the old connection used again after 30 s of silence:
```
4066   toolCall book_slot id=call_366014 ; 5068 freeze ; 6958 client rule fires, no resume
8068   service commits BK-1001 (response held)
35070  unfreeze (+30.0 s): old connection OPEN, server sends nothing for 3 s
38074  toolResponse for call_366014 on the same old connection
38791  model   "I'm booking the 3 p.m. slot for you for tomorrow."
41078  user    "Did you book it?"
42633  model   "Yes, the 3 p.m. slot for tomorrow has been successfully booked."
```

### Limits of round 2

- N=3 and N=2 per scenario, and N=1 for P4. One network path, one model, and all runs in one evening.
- The freeze is application-level (see "Faithfulness" above). A real loss differs at the TCP level. With an idle model it probably does not differ in what the server observes, but a server using TCP keepalive would end a real dead connection sooner than ours.
- The client was never told by the server; the loss was detected only by the client's own rule. Only one rule was tested (0.5 s ping, 2.0 s silence).
- In every run the model was idle at the freeze (R1 point). A freeze during model speech was not tested; there, the server's writes would stall instead of having nothing to send.
- Nothing spoken during the gap was buffered or replayed.

## What a client must do

These are design consequences of what we saw in this round, not guarantees from the API.

1. **The client owns the call ledger, keyed by call id, across connections.** A drop does not cancel or re-issue a pending call. The server kept it pending and accepted its response on the next connection. In round 2 the call stayed pending up to 154 s after it was issued, and its response was still accepted. The client keeps executing (or holding) the side effect under its own policy, and sends the result for the old id as soon as a resumed connection is up.
2. **Always send the outcome after a resume, even if the side effect finished during the gap.** Without it, the model goes on saying "I'm booking it now" after the booking is made (R2). What the model says is only as fresh as the last tool response.
3. **Retry the resume, but do not count on retries alone; do not treat 1011 as a dead handle.**
   - When the server has seen the close (round 1): an immediate attempt after an idle drop failed every time, and waiting about 1.5 s, or retrying after 1 s, worked every time. Expect about 2.1 to 2.3 s of silence after an idle drop and about 0.6 s after a drop during speech, and design the audio side (a filler cue, a buffer) for that.
   - When the server has not seen a close (round 2): the same `1011` comes back for as long as the old connection is open on its side: 40 of 40 attempts up to 133 s in N1 and N2. In P4 nothing worked for 12.8 minutes: some attempts hung for 10 s instead of failing, and once the server dropped the session, `1011` meant the handle was dead. A `1011` that persists means the old connection still holds the session; see items 10 and 11.
4. **Keep the handle you have; do not wait for a fresh one.** Only one handle arrived per connection, at its start, and it restored the latest state. A client that waits for a `resumable: true` update after the call, or reads a missing update as "not resumable", would never resume. `resumable: false` during a function call never appeared.
5. **Do not count on transparent mode or `lastConsumedClientMessageIndex` on the Gemini Developer API.** Neither the SDK nor the server accepts it. If audio spoken during the gap matters, buffer it yourself; this round did not test whether replaying it works.
6. **Expect speech that was cut by the drop to be redone.** After a drop during speech, the model spoke a sentence that repeated part of the cut one (R4 runs 2 and 3). A client that already played the pre-drop audio will play part of it twice.
7. **Do not wait for `goAway`.** None arrived in short sessions, nor on the frozen connections of round 2 (12.8 minutes in P4, where the server ended the frozen connection with a `1011` close instead). Unplanned drops come without warning, so the resume path must work without one.
8. **Deduplicate by business key anyway.** No call was re-issued here (0/14), but the earlier stop test saw re-issues after interruptions in BLOCKING mode. The canonical key (`tomorrow 15:00`) already absorbs the argument drift seen here (`tomorrow at 3pm` against `tomorrow 3pm`).
9. **Detect the loss yourself, with pings (round 2).** With the model idle, the server sends nothing for long stretches, and it never pinged the client. It does answer WebSocket pings (median RTT 10 to 25 ms). A ping every 0.5 s, with the link declared lost after 2 s without any frame or pong, detected every freeze 1.8 to 1.9 s after it started. A rule based on frames alone would misfire on a healthy idle link. If you keep websockets' default keepalive (20 s, 20 s), it may close a frozen socket by itself 20 to 40 s later; that only helps if the close can reach the server.
10. **Get a close to the server; do not just drop the socket (round 2).** The resume only worked once the server had seen the old connection close (5/5, about 0.6 s after the client's close 1000, for losses of 49 to 145 s). On a detected loss, keep the old socket, send a close frame on it, and resume with backoff. If the path comes back, the close should go through with the queued data, and the next attempt should succeed. In N1 and N2 the close was sent after the path came back; sending it while the path is still down was not tested. If the path comes back before you resume, the old connection is still fully usable (N3): sending the pending response on it is also an option. Not tested: whether a local abort, whose RST is lost on a dead path, ever reaches an idle server. Nothing in round 2 suggests that it would.
11. **Plan for a lockout when the old path never returns (round 2).** Typical case: a Wi-Fi to mobile switch, which changes the client's address. Then no close can reach the server, and in P4 the session stayed locked (attempts refused, then hanging) until the server closed the old connection with `1011` after about 8 minutes. After that the session could not be resumed at all (N=1). Since nothing worked in the 12.8 minutes we tried, a short cut-off (set by the product: seconds to tens of seconds) gives up very little. A client needs a cut-off and a fallback: start a new session (no handle), rebuild the context from its own ledger and transcript (`send_client_content`), and reconcile the pending side effect itself. The old session's call can no longer be answered in that case.

## Limits of round 1

- N=3 per scenario and N=1 per probe and control, with one network path and one model. All sessions ran between 22:06 and 22:16 local time on one day.
- The sessions were short (at most 23 s). Handle refreshes, `goAway` and resumption windows over minutes or hours were not exercised.
- The drop was simulated on the client: the transport was aborted, so the server saw the TCP connection close. In a real network loss the packets just stop, and the server may not notice for a long time. Round 1 did not test that case; round 2 tests an approximation of it.
- Nothing was sent during the gap. No audio was buffered or replayed.
- The reason for the 1011 was not measured. It depends on whether the model was idle at the drop (12/12 against 0/3), not on the close type (P2) or on the failed attempt itself (P3). The cause is server-side and unknown.
- R1 run 1 ran before the retry logic existed. Also, in the R0 to R4 runs, one `audio_segment` summary event labels with connection 1 a model audio segment that continued on connection 2. That is cosmetic (the per-chunk events are correct), and it was fixed before P2.
- Side note from earlier data: the stop-test logs show one `sessionResumptionUpdate` per session (at about 400 ms) even though those sessions did not set `sessionResumption`. [GD-REF] says updates are sent only when it is set. The content was not logged then.

## Round 3: real packet loss

Round 3, 2026-10-03, 09:56 to 11:15 local time; same model, SDK and harness, with the client in a Linux container. It used 6 of the 8 budgeted sessions (B1 x3, B2 x2, B3 x1). There was no quota or billing error.

### Short answer

- **A real silent loss locks the session out for good when the old path does not come back.** None of the 5 runs without a path back (B1 x3, B2 x2) was ever resumed. 450 of 450 attempts, made at detection and then every 10 s up to +892 s, were refused with `1011 Internal error encountered.`, 460 to 1863 ms after they started (median 562 ms). No attempt hung. The booking, committed 1 to 3 s after the loss, was never reported to the user.
- **The server never probed the dead client, and its TCP did not free the session.** With the model idle (B1), the server sent nothing for 479 s: no TCP keepalive, no WebSocket ping. At +479.0 to +479.4 s it sent one 53-byte segment, the size of round 2's close frame. That was 480.0 to 480.4 s after its last message, the toolCall turn (3/3). With no ACK, it retransmitted that segment 11 times, backing off from 0.22 s to a cap of 30.2 s; the last copy came at +596 s. Then nothing more arrived, and no RST or FIN. The attempts were refused the same way before that segment, during its retransmissions, and after the server's TCP had given up.
- **Unacknowledged data in flight changes nothing (B2).** In one run 39.6 KB of model audio went into the blackhole mid-burst; in the other, one 512-byte end-of-turn segment. In both, the server's TCP retransmitted on the same schedule for 115 to 116 s, then went silent, about 2 minutes after the loss. Attempts stayed refused for 15 minutes, before and after that point.
- **If the old path comes back, the client must still close the old connection (B3, N=1).** The rule was removed after 30 s. The client kernel sent its 66 KB backlog at +53.2 s, at its next probe. The server answered with three small records (consistent with pongs for the queued pings) and no data. Attempts stayed refused while the old connection was open, including 3 made after the backlog had gone through. The client's close 1000 was echoed in 14 ms, and the next attempt worked 507 ms later. The response for the call issued 83.6 s earlier was accepted, and the answer matched the service.
- **This confirms round 2 and removes its main caveat.** A real dead link was not detected sooner by TCP. With an idle model the server has nothing to send, so TCP has nothing to time out. With data in flight, the server's TCP gives up after about 2 minutes, but the session stays locked. Two details of round 2 were not reproduced; see "Round 3 against round 2".

### Method

- **Where the client runs.** The harness runs in a Linux container (`Dockerfile`): python:3.13-slim with the same `uv.lock` as rounds 1 and 2 (google-genai 2.25.0, websockets 16.1.1), plus iptables 1.8.11 (nf_tables), tcpdump 4.99.5 and iproute2. The `.env` is not in the image; it is mounted read-only at run time. The container runs on Colima 0.9.1 (vz) on this Mac, started with `--cap-add NET_ADMIN`. `results/` is mounted, so the JSONL files and the session ledger are the same as in rounds 1 and 2.
- **The network path had to be fixed first.** With Colima's default network, the VM goes out through Lima's user-mode network (gvisor-tap-vsock). There, `lsof` on the Mac showed the TCP connection to `172.217.114.4:443` owned by `limactl`. So a process on the Mac terminated Google's TCP and would have kept ACKing: round 2's caveat again. Colima was therefore restarted with `--network-address --network-preferred-route`, which routes the VM through Apple's NAT (vzNAT, interface `col0`; no root needed). The same check then showed no Mac process holding the connection. Google's SYN-ACK reached the container with TTL 119 and MSS 1412, so it came from Google's own stack across the real path. Between the container and Google there are only packet-level NATs: Docker's in the VM, vmnet's on the Mac, and the home router's. Nothing on macOS was configured by hand, and the profile's `colima.yaml` was restored afterwards.
- **Blackhole** (`blackhole.py`). At the drop point, the harness reads the live connection's 4-tuple from the websocket transport (`sockname`, `peername`). It then inserts two iptables jumps in the container's network namespace: OUTPUT for `-p tcp -s <local> -d <remote> --sport <lport> --dport 443`, and INPUT for the reverse. Each jump leads to a chain that counts RST and FIN segments in their own rules before a catch-all DROP. DROP answers nothing (no RST, no ICMP), and nothing is closed on either side. A resume opens a new connection with a new source port, which passes. This models a phone that lost its Wi-Fi and came back on another path.
- **What was observed on the old flow.**
  - tcpdump on the container's `eth0`, headers only (80-byte snap, `--immediate-mode`). Inbound segments are captured before netfilter drops them, so every server segment that arrives during the blackhole is timestamped with its flags, sequence range and length. Outbound segments are captured only if they pass netfilter, so a client segment on the wire would be a leak. There were none.
  - Logged every second, on change: the chain counters, `ss -tino` for the client socket, and the old websocket's state.
  - From the blackhole on, the old websocket is read raw (as in round 2's drain), so any frame or close that reaches it is logged.
- **Client side: same rules as round 2.** websockets' keepalive is off. The harness pings every 0.5 s and declares the link lost after 2.0 s without a frame or a pong. The old socket is then left open and unused. Resume attempts use the handle, at detection, then every 10 s start to start. No attempt starts later than 15 min after the blackhole, and each attempt has 10 s to reach `setupComplete`. On success, the held response for the old call id is sent, then "Did you book it?" is asked, as in round 1.
- **Scenarios.**
  - B1 (N=3): R1 point, 1.0 s after the `toolCall`, model idle; the commit comes 4 s after the call.
  - B2 (N=2): R4 setup: the follow-up question 0.5 s after the call, commit 7 s after the call. Run 1 dropped at R4's point, 1.0 s after the first model audio chunk. That point turned out to be after the server had sent the whole reply: 13 chunks, 224 KB, 4.7 s of audio, delivered in 0.88 s, with `generationComplete` 100 ms before the blackhole. In round 1's R4, audio still arrived 1 s after the first chunk. So that run left only a 512-byte segment unacknowledged on the server side. Run 2 therefore dropped 0.2 s after the first chunk (`--disconnect-after 0.2`), inside the burst. The first invocation was stopped after its run 1 (`docker kill`, in the 3 s between runs), and run 2 was started with `--first-run 2`. Both runs' summary rows are in `B2.jsonl`; `results/summary.md` is rebuilt from the JSONL files by `make_summary.py`.
  - B3 (N=1): R1 point. The rule is removed 30 s after the blackhole. Attempts continue every 10 s with the old connection open for 60 s more. Then the client closes the old connection (close 1000) and resumes with the round-1 schedule.

Offline check (`test_blackhole.py`): two containers on the Docker bridge, no key, no Internet. The client container has NET_ADMIN; the server has its own network namespace and no capabilities, and reports its own socket state through a second, unblocked connection.
- **During a 6 s blackhole with traffic both ways:**
  - The client got 0 messages, saw no close, and stayed OPEN.
  - The server got 0 of the 20 client messages and saw no close.
  - On the client's `eth0`, 8 server segments arrived, all of them dropped (INPUT counter 8), and 0 client segments left (OUTPUT counter: 24 attempts).
  - The server socket stayed ESTABLISHED with retransmissions 1 to 5 and growing backoff; RST and FIN counters stayed at 0.
- **After the rule was removed,** the backlog went through both ways: the server got 20 of 20, the first 0.32 s later.
- **When the server closed during a blackhole** (a close frame, then a RST 1 s later), the client saw nothing and stayed OPEN. The RST was counted and dropped. After removal, the client's next message met the server's dead socket, and the client saw the connection end.
- **One difference from a real loss, on the client side only.** With an OUTPUT drop, the client's own stack sees its sends fail locally. So the client socket runs its probe timer (`ss`: `persist`) instead of the retransmission timer. Both back off exponentially, and neither puts a segment on the wire. The server side sees exactly a dead peer.

Reproduce:
```sh
colima start --network-address --network-preferred-route    # vzNAT; see Method
docker build -t r3-blackhole .
docker run -d --rm --name bh-server r3-blackhole python test_blackhole.py server
docker run --rm --cap-add NET_ADMIN r3-blackhole python test_blackhole.py client \
    $(docker inspect -f '{{.NetworkSettings.IPAddress}}' bh-server)       # offline
R="docker run --rm --cap-add NET_ADMIN -v $PWD/.env:/app/.env:ro -v $PWD/results:/app/results r3-blackhole python resume_test.py --budget 34"
$R --scenario B1 -n 3
$R --scenario B2 -n 1                                    # run 1 (R4 point)
$R --scenario B2 -n 1 --first-run 2 --disconnect-after 0.2
$R --scenario B3 -n 1
```
`resume_test.py --fake-live ws://<host>:<port>/ws` (with a dummy key and a scratch `--results-dir`) was used to dry-run B1 to B3 offline against a small fake Live server before any live session.

### B1, blackhole while the model is idle (R1 point)

"Server on the old flow" lists every server segment that reached the container while blackholed. Times are from the blackhole.

| run | blackhole | detected | resume attempts while blackholed | first accepted resume | server on the old flow | client socket at the end | booking |
|---|---|---|---|---|---|---|---|
| 1 | +1013 ms after the call, model idle | +1739 ms | 90, at +1.7 to +891.7 s, all `1011` after 498 to 646 ms | none in 15 min (lockout) | nothing until +479.4 s; then one 53 B segment, retransmitted 11 times until +596.5 s; then nothing; 0 RST, 0 FIN | ESTABLISHED, 78.9 KB unsent, probe backoff 15 | committed @8430 (+3.0 s), never reported |
| 2 | +1009 ms | +1515 ms | 90, at +1.5 to +891.5 s, all `1011` after 485 to 1245 ms | none in 15 min | 53 B at +479.0 s, last retransmission at +596.4 s; 0 RST, 0 FIN | ESTABLISHED, 65.8 KB unsent, backoff 15 | committed @8052 (+3.0 s), never reported |
| 3 | +1004 ms | +1549 ms | 90, at +1.5 to +891.5 s, all `1011` after 460 to 837 ms | none in 15 min | 53 B at +479.0 s, last retransmission at +596.2 s; 0 RST, 0 FIN | ESTABLISHED, 70.1 KB unsent, backoff 15 | committed @8030 (+3.0 s), never reported |

### B2, blackhole with the call pending and the model replying (R4 setup)

| run | blackhole | model at the blackhole | detected | resume attempts | first accepted resume | server on the old flow | booking |
|---|---|---|---|---|---|---|---|
| 1 | 1.0 s after the first audio chunk (+6021 ms after the call) | reply already fully sent (see Method) | +1926 ms | 90, at +1.9 to +891.9 s, all `1011` after 492 to 674 ms | none in 15 min | one 512 B segment at +3.65 s (when the 4.7 s of audio would have finished playing), retransmitted 11 times until +119.3 s; then nothing; 0 RST, 0 FIN | committed @11149 (+1.0 s), never reported |
| 2 | 0.2 s after the first audio chunk (+4943 ms) | mid-burst: 4 chunks (36 KB) had arrived | +1929 ms | 90, at +1.9 to +891.9 s, all `1011` after 474 to 1863 ms | none in 15 min | 39.6 KB of new data in the first 0.15 s (17 segments), a tail-loss probe, then the first segment retransmitted 10 times until +114.8 s; then nothing; 0 RST, 0 FIN | committed @11087 (+2.1 s), never reported |

The model's words before the blackhole: run 1, the whole sentence "I am booking that slot for you. Please bring any relevant documents or identification."; run 2, "I am booking your 3".

### B3, blackhole for 30 s, then the old path comes back (N=1)

| time after the blackhole | what happened |
|---|---|
| +1.5 s | client rule fires (no frame and no pong for 2004 ms); attempt 1 |
| +1.5, +11.5, +21.5 s | 3 attempts, rule active: each `1011` after 504 to 547 ms |
| +30.0 s | rule removed. The client socket is in probe backoff (`persist`, 25 s at +27.4 s) |
| +31.5, +41.5, +51.5 s | 3 attempts, old path back but idle: each `1011` (509 to 569 ms) |
| +53.2 s | the client kernel's next probe: its backlog (65,796 B of mic audio and 3 pings queued before detection) goes out at once. The server ACKs it and sends three 28-byte records (consistent with the 3 pongs) and no data frame. The old websocket stays OPEN |
| +61.5, +71.5, +81.5 s | 3 more attempts, with the old connection carrying traffic again: each `1011` (489 to 552 ms) |
| +82.0 s | client closes the old connection (close 1000); the server echoes 1000 in 14 ms |
| +82.6 s | resume, first attempt: `setupComplete` 507 ms after the close; the response for the old call id is sent at once and accepted |

Result: no re-issued call, no `toolCallCancellation`, no `goAway`, 1 commit (@8120, +3.0 s), answer consistent.

### Server-side TCP on the blackholed flow

- **Retransmission schedule,** the same in all 5 runs that had something to retransmit: about 0.21 to 0.22 s, then doubling (0.44, 0.9, 1.8, 3.5, 7.2, 14.3, 28) up to a cap of 30.2 s.
- **Then silence.** The last copy reached the container 114.7 to 117.2 s after the first transmission, and nothing came after it: no further retransmission, no RST, no FIN. The next copy would have been due 30 s later, so the server's TCP gave up about 2.5 minutes after it started sending, without telling the client.
- **With an idle model (B1),** nothing at all was sent for 479 s.
- **Path state.** The NAT mappings along the path survived 479 s of total silence, since the 53-byte segment still reached the container.
- **The attempts went to at least 7 different front-end addresses** (172.217.112.4 to 172.217.119.4), with the same refusal.

### Transcripts, round 3 (verbatim, from the JSONL)

B1 run 1, real loss with the model idle, never resumed:
```
4423    toolCall book_slot {"slot": "tomorrow 3pm"} id=call_663882        (turn_complete at once)
5436    blackhole: old flow dropped both ways, nothing closed
7175    client rule fires (no frame and no pong for 2026 ms); resume #1 -> close 1011 "Internal error encountered." after 586 ms
8430    service commits BK-1001 (response held, never delivered)
...     resume #2 to #90, every 10 s up to +891.7 s -> 1011 each time (498 to 646 ms)
484800  first server segment since the blackhole (+479.4 s): 53 B; retransmitted 11 times until +596.5 s, then nothing
897799  run ends: lockout; the client's old socket is still ESTABLISHED
```

B3, path back after 30 s, old connection closed by the client, then resumed:
```
4103    input   "Put me the 3:00 p.m. slot tomorrow, please."
4106    toolCall book_slot {"slot": "tomorrow 3pm"} id=call_945202
5116    blackhole
6614    client rule fires; resume #1 -> 1011 after 547 ms ; #2, #3 at +11.5, +21.5 s -> 1011
8120    service commits BK-1001 (response held)
35159   rule removed (+30.0 s) ; resume #4 to #6 at +31.5, +41.5, +51.5 s -> 1011
58330   client backlog sent on the old flow; the server answers with 3 small records, no data frame
...     resume #7 to #9 at +61.5, +71.5, +81.5 s -> 1011
87162   client closes the old connection (1000); server echo at 87176
87683   resume #10 -> setupComplete {} (507 ms after the close)
87690   toolResponse for call_945202 (issued 83.6 s earlier) on the new connection
88389   model   "I am booking the 3 p.m. slot for you tomorrow."
92501   user    "Did you book it?"
92515   model   "Yes, the 3 p.m. slot for tomorrow has been successfully booked."
```

### Round 3 against round 2

- **Confirmed:**
  - Resumes are refused with `1011` for as long as the server holds the old connection.
  - The server never pings or probes a silent client.
  - Closing the old connection unblocks the resume at once.
  - The pending call survives as long as the session does: its response was accepted 83.6 s after the call.
  - Detection with the round-2 rule took 1498 to 1929 ms (n=6).
- **Caveat removed:** round 2 asked whether TCP would detect a real dead link sooner than its ACKing proxy. It does not. When the server has nothing to send, nothing is detected. When it has data in flight, its TCP gives up after about 2 minutes, but the session is not released.
- **Not reproduced, explicitly:**
  1. Round 2's P4 had a phase of attempts that hung for 10 s (+244 to +454 s). Here 0 of 459 attempts hung, including over the same time range.
  2. P4 saw the server's close at about +489 s; here the 53-byte segment came at +479.0 to +479.4 s (3/3).

  Whether P4's hangs and its later close came from the proxy or from server behavior on that evening is unknown.
- **Extended:**
  - B3 shows that the old path coming back is not enough. Once the client's backlog had reached the server on the old connection, attempts were still refused until the client closed it.
  - In round 2 the old connection was always closed before resuming after the unfreeze, so that case had not been tested.

### What a client must do: changes from round 3

Items 1, 2 and 4 to 9 are unchanged. Round 3 changes the following:

- **Item 3,** addition: a persistent `1011` looks the same whether the server still holds the session or has already dropped it. In B1 the refusals were identical before the server's close segment at +479 s, during its retransmissions, and after its TCP gave up, and they came from at least 7 front-end addresses. So the client cannot learn anything from the refusal itself.
- **Item 10,** confirmed with a real loss, with two additions.
  1. When the path comes back, the client must still close the old connection; the path coming back is not enough (B3).
  2. The close, like everything else queued on the old socket, leaves only when the client kernel next retransmits or probes. After 30 s of loss that timer had backed off to 25 s, so the backlog left 23 s after the path came back.

  So a close queued at detection (round 2's advice) helps only on a path that returns, and only after the kernel's own backoff.
- **Item 11,** confirmed with a real loss and made stronger: 5 of 5 sessions were lost when the old path never came back. Nothing on the server side released them within 15 minutes: not its TCP giving up at about 2.5 minutes (B2), and not its close at about 8 minutes (B1). Retrying for longer than the product's cut-off gained nothing in 450 attempts. Typical case: a phone that switches networks. The client should do three things:
  1. Use a short cut-off, seconds to tens of seconds, set by the product.
  2. Then start a new session and rebuild the context from its own ledger and transcript.
  3. Settle the side effect itself: in every lockout run the booking had been made before the user could be told.

### Limits of round 3

- N=3, N=2 and N=1. One network path (Google front end about 6 ms away, client socket RTT 5.7 to 6.5 ms), one model, one morning.
- The two B2 runs are different conditions. Run 1 left a 512-byte segment unacknowledged; run 2 left 39.6 KB of audio. Round 1's R4 point no longer fell during the server's send burst, because today's reply arrived faster than real time.
- Client side: an OUTPUT drop makes the client socket use its probe timer instead of retransmitting (see the offline check). This only affects when the client's backlog left in B3 (+53.2 s). The server side sees a dead peer either way.
- The 53-byte segment is identified as a close frame by its size and timing, which match P4's decoded close (53 B, `1011 Internal error encountered.`). TLS was not decrypted.
- Not tested:
  - whether fewer or later attempts change the outcome (round 2's P4 tried every 60 s and also never succeeded);
  - a path that comes back after the server's close or after its TCP gives up;
  - replaying speech spoken during the gap.
- Setup: Colima was not running before round 3. It was started for it (`colima start`), which needed `limactl disk unlock colima` first to clear a stale disk lock left by a failed start on 2026-08-28. It was then restarted with vzNAT. At the end it was stopped, its `colima.yaml` restored, and the Docker context set back to `desktop-linux`.

## Stage 2: client-side recovery

Stage 2, 2026-10-03, 11:38 to 11:46 local time; same model, SDK, container and blackhole as round 3. It used 11 of the 12 budgeted sessions (BR1 x4, BR2 x3, ablation x2, control BR0 x2), counted as in rounds 1 to 3: one ledger line per run, although each recovery run also opened 4 refused resume connections and one new session. There was no quota or billing error.

### Short answer

- **The recovery got the truth to the user in 9 of 9 runs.** After a real silent loss with the booking pending, every resume in the 4 s window was refused (36 of 36, `1011 Internal error encountered.`, 472 to 642 ms each). The client then opened a new session, whose `setupComplete` came 249 to 318 ms later, and restored the conversation from its own ledger. The answer to "Did you book it?" matched the service in 9 of 9 runs (BR1 4/4, BR2 3/3, ablation 2/2), with exactly 1 commit per run.
- **With the status note, the model's first reply already said the booking was made, and it never re-issued the call** (0 of 7 runs). For example: "Of course, your 3:00 p.m. slot for tomorrow has been successfully booked." That is the transcript; see the limits for how much of it would have played.
- **Without the note (summary only, N=2), the model re-issued `book_slot` in 2 of 2 runs**, 577 and 858 ms after the restore, before saying anything. The client deduplicated it by business key and answered it from the committed job (BK-1001), so there was still 1 commit and the final answer was right. But the model's first sentence was "I'm booking the 3:00 p.m. slot tomorrow for you.", 3.6 to 3.8 s after the commit. So the note made the first reply right and avoided the re-issue; the dedupe is what kept the re-issue from becoming a second booking.
- **User-visible gap.** From the blackhole to the first model audio of the recovered session: 6.10 to 7.09 s (BR1) and 6.52 to 7.08 s (BR2). It breaks down as:
  - detection: 1.49 to 2.04 s;
  - the resume window: 3.55 to 3.60 s (the last attempt starts at +3.0 s and is refused about 0.55 s later);
  - the new session's setup: 0.25 to 0.32 s;
  - the first model audio: 0.76 to 1.20 s after the restore.

  From the last audio heard before the loss: 8.24 to 9.32 s in BR1 (the end of the user's request, since the model had said nothing after its tool call) and 5.95 to 6.06 s in BR2 (the end of playback of the cut reply). The resume window, which never succeeded in this condition, is the largest share.
- **The sentence cut by the loss (BR2) was neither lost nor repeated word for word.** In 3 of 3 runs, the model's first reply in the new session said the booking was confirmed, then answered the question the cut reply had started on: "Please bring your ID and any relevant documents." The cut "I'm booking that slot now" was not repeated. In this script, the user's next question came before that reply had finished playing; see the limits.
- **The control (BR0) locked out as in round 3.** A plain resume loop every 1 s for 60 s: 118 of 118 attempts refused, no session, and the booking (committed 4.0 s after the call) was never reported.
- **The close sent on the old socket at detection never left the client**: 0 FIN, 0 RST, close never acknowledged. websockets gave up after its 10 s close timeout. The socket then sat in FIN-WAIT-1 with 66 to 92 KB unsent. On a dead path it costs nothing and does nothing, as round 3 predicted.

### Design: `recovery.py`

One module, a reference pattern like `commit_guard.py`. The harness mode is `--scenario BR1|BR2` in `resume_test.py`. The client keeps a ledger with two parts:
- its transcript, from input and output transcription: consecutive chunks of one role form a turn, and `turnComplete` or `interrupted` closes it;
- the side effects, keyed by call id, with their state from the service.

1. **Detect.** The rule is unchanged since round 2, run by the harness's `liveness()`: a ping every 0.5 s, and the link is lost after 2.0 s without a frame or a pong.
2. **Resume window.** Attempts with the last handle: one at detection, then one every 1.0 s, start to start. None starts after 4.0 s, and an attempt still open at the end of the window is abandoned. In parallel, the SDK context of the old connection is exited, so websockets queues a close (1000), waits its 10 s close timeout, then closes the socket.
3. **Fallback.** Open a new session: same config, no handle, so it gets its own resumption handle. Send one `send_client_content(turns=[...], turn_complete=True)`:
   - the last 6 transcript turns as `user` and `model` turns; a model turn still open at the loss gets " [cut off here by the connection loss]";
   - then one `user` turn: a system note with an intro, one status line per side effect that is pending or finished in the last 300 s (from the service ledger), and an outro.

   `turn_complete=True` makes the model speak at once, which is what tells the user something and is how the gap is measured. The pending call's id is never answered: it is marked orphaned. If the job finishes after the restore, an update note is sent once the model is idle. A `book_slot` issued in the new session is matched by business key and answered from the existing job with `duplicate_of`; it is never executed. The key is commit_guard's: the tool name plus the sorted JSON of `canonical_slot`, within 600 s.
4. **Gap.** A playback model stands in for the speaker: chunks play back to back at 24 kHz from their arrival, and `interrupted` flushes them. "Last heard" is the later of two times: the end of the user's last utterance before the loss, and the end of playback of the model audio received on the old connection. That audio is counted up to detection, because frames already in the client kernel are read just after the blackhole. The gap runs from "last heard" to the first audio chunk of the recovered session.

Scenarios:
- BR1 uses the B1 point: blackhole 1.0 s after the `toolCall`, model idle, commit 4 s after the call.
- BR2 uses the point of round 3's B2 run 2: the follow-up question 0.5 s after the call, blackhole 0.2 s after the first model audio chunk (inside the burst), commit 7 s after the call.
- In both, the ask comes once the model has reacted to the restore, with the same settle rule as round 1: at least 3 s, model idle, 1 s without output.
- The ablation is BR1 with `--no-status-note`. The intro and outro of the note are still sent; only the status lines go.
- BR0 is B1 with `--bh-period 1 --bh-max 60`.

In `resume_test.py`, the blackhole steps shared by B1 to B3 and BR1/BR2 moved into one helper, `blackhole_until_detect()`. An offline B1 dry run gave the same output as before, and BR0 ran that code live.

Offline checks before any live session:
- `test_recovery.py`, with a fake `connect()` and a fake session, covers:
  - the window, including the cut at its end;
  - the fallback, the cut marker and the note texts;
  - a job still pending at the restore, then the update note;
  - a pending duplicate, answered when the job commits;
  - the ablation;
  - a resume accepted inside the window;
  - a quota error stopping the recovery.
- Container dry runs against a fake Live server (dummy key) for BR1, BR2, a drifted re-issue ("tomorrow at 3pm", deduplicated), the ablation, the pending path, and B1.

### Texts actually sent

The ASR transcript of `book.wav` ("Book me the 3pm slot tomorrow, please.") was "Put me the 3:00 p.m. slot tomorrow, please." in every run, as in rounds 1 to 3. The client restores what it heard, errors included.

Summary turns:
- BR1 and the ablation: `user`: "Put me the 3:00 p.m. slot tomorrow, please."
- BR2, in three turns:
  - `user`: "Put me the 3:00 p.m. slot tomorrow, please."
  - `user`: "While you do that, what should I bring to the appointment?"
  - `model`: run 1 "I'm booking that slot now. Please [cut off here by the connection loss]"; run 2 "I'm booking that slot now; please [cut off here by the connection loss]"; run 3 "I'm booking that slot [cut off here by the connection loss]".

Note (BR1 run 1; the other runs differ only by the time; the time is the service's commit time, and the container's clock was set to Europe/Paris):
```
System note: the voice connection to the user was lost and the conversation continues in a new session. The turns above are the last turns before the loss, from the app's own transcript. The booking for tomorrow 3pm (BK-1001) was confirmed at 11:38:48 CEST during the connection loss. Tell the user it is confirmed if they ask. Continue the conversation from where it stopped.
```
Note in the ablation:
```
System note: the voice connection to the user was lost and the conversation continues in a new session. The turns above are the last turns before the loss, from the app's own transcript. Continue the conversation from where it stopped.
```
No update note was needed in any live run, because the commit always landed during the resume window. The "still being processed" line and the update note after a later commit were exercised offline only.

Reproduce:
```sh
colima start --network-address --network-preferred-route    # vzNAT, as in round 3
docker build -t br-recovery .
uv run test_recovery.py                                       # offline
R="docker run --rm --cap-add NET_ADMIN -e TZ=Europe/Paris -v $PWD/.env:/app/.env:ro -v $PWD/results:/app/results br-recovery python resume_test.py --budget 44"
$R --scenario BR1 -n 1 ; $R --scenario BR1 -n 3 --first-run 2
$R --scenario BR2 -n 3
$R --scenario BR1 -n 2 --no-status-note --name BR1_nonote
$R --scenario B1 -n 2 --bh-period 1 --bh-max 60 --name BR0
```

### BR1, blackhole while the model is idle (B1 point), recovery on

Times are from the blackhole unless noted. "First audio" is counted from the restore. The gap is given twice: from the last audio heard, then from the blackhole. Every run had 4 resume attempts in the window, all refused with `1011`. In every run the commit came 4.0 s after the call (BK-1001), during the window, and there was no re-issued call.

| run | blackhole (vs call) | detected | refused in | new session `setupComplete` | first audio | model after the restore (verbatim) | answer to "Did you book it?" | commits | consistent | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | +1015 ms | +1489 ms | 550 to 587 ms | +5.32 s (+3.83 s after detection) | 770 ms | "Of course, your 3:00 p.m. slot for tomorrow has been successfully booked." | "Yes, your slot tomorrow at 3:00 p.m. is officially booked." | 1 | yes | 8.24 s; 6.10 s |
| 2 | +1015 ms | +1545 ms | 559 to 578 ms | +5.38 s (+3.84 s) | 846 ms | "I have successfully confirmed your booking for tomorrow at 3:00 p.m." | "Yes, the slot for tomorrow at 3 00 PM is confirmed." | 1 | yes | 8.40 s; 6.23 s |
| 3 | +1015 ms | +2010 ms | 496 to 584 ms | +5.88 s (+3.87 s) | 1203 ms | "Everything is confirmed for tomorrow at 3:00 p.m." | "Yes, I've booked the 3:00 p.m. slot for you tomorrow." | 1 | yes | 9.32 s; 7.09 s |
| 4 | +1005 ms | +1538 ms | 486 to 580 ms | +5.39 s (+3.85 s) | 809 ms | "I've booked that slot for you. It's now confirmed." | "Yes, the 3:00 p.m. slot for tomorrow is successfully booked." | 1 | yes | 8.33 s; 6.20 s |

### BR2, blackhole while the model is speaking with the call pending

"Cut reply" is the model's text received before the loss, with the audio received for it. Between 20 and 44 KB of further model audio went into the blackhole (the server retransmitted it on round 3's schedule). In every run the commit came 7.0 s after the call, during the window, and there was no re-issued call.

| run | blackhole (vs call; vs first model audio) | detected | cut reply | refused in (4 attempts) | new session `setupComplete` | first audio | model after the restore (verbatim) | answer to "Did you book it?" | commits | consistent | gap |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | +5371 ms; +215 ms | +2035 ms | "I'm booking that slot now. Please" (1.23 s of audio) | 581 to 609 ms | +5.92 s (+3.88 s) | 1157 ms | "To confirm, your slot for tomorrow at 3:00 p.m. is booked. Please bring any relevant identification or documents." | "Yes, your 3:00 p.m. slot for tomorrow is fully booked." | 1 | yes | 6.06 s; 7.08 s |
| 2 | +4929 ms; +206 ms | +2027 ms | "I'm booking that slot now; please" (1.15 s) | 503 to 582 ms | +5.89 s (+3.86 s) | 1057 ms | "It's confirmed! Please bring your ID and any relevant documentation to the appointment." | "Yes, your appointment for tomorrow at 3:00 p.m. is officially booked." | 1 | yes | 6.01 s; 6.95 s |
| 3 | +5035 ms; +215 ms | +1946 ms | "I'm booking that slot" (0.79 s) | 472 to 584 ms | +5.76 s (+3.81 s) | 758 ms | "Your booking for tomorrow at 3:00 p.m. is confirmed. Please bring your ID and any relevant documents." | "Yes, the booking for tomorrow at 3:00 p.m. is officially confirmed." | 1 | yes | 5.95 s; 6.52 s |

In BR2 run 1, the second transcript chunk ("slot now. Please") and the last audio chunk were already in the client kernel at the blackhole and were read 7 ms after it. Its gap (6.06 s) is recomputed from the JSONL audio segments by `make_summary.py`, which writes it into `results/summary.md` and says why. The `run_end` row stored at run time says 6.54 s, because it used a snapshot taken at the blackhole; the harness was fixed before the later runs, which take the snapshot at detection.

### Ablation: BR1 without the status note (summary only)

| run | blackhole (vs call) | detected | refused in (4 attempts) | new session `setupComplete` | re-issued call and dedupe | first audio | model after the restore (verbatim) | answer to "Did you book it?" | commits | consistent | gap |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | +1012 ms | +1501 ms | 567 to 642 ms | +5.32 s (+3.82 s) | `book_slot {"slot": "tomorrow 3pm"}` 577 ms after the restore, before any speech; same key as the committed job; answered 3 ms later with its result (BK-1001, `duplicate_of`); not executed | 1240 ms | "I'm booking the 3:00 p.m. slot tomorrow for you." | "Yes, the 3:00 p.m. slot tomorrow is officially booked." | 1 | yes | 8.71 s; 6.56 s |
| 2 | +1011 ms | +1485 ms | 517 to 566 ms | +5.29 s (+3.81 s) | same, 858 ms after the restore; answered 2 ms later | 1525 ms | "I'm booking the 3:00 p.m. slot for you tomorrow." | "Yes, your 3:00 p.m. slot for tomorrow is all booked." | 1 | yes | 8.97 s; 6.82 s |

The model's sentence after the deduplicated response is the one the system prompt asks for after a call. Round 1 saw the same sentence after the old response (8 of 11). Here the model said it although the response it had just received carried `"status": "booked"`. Without the dedupe, the harness would have executed the re-issued call and committed a second booking; that combination was not run.

### BR0, control: plain resume loop for 60 s, no fallback

| run | blackhole (vs call) | detected | attempts | first accepted resume | server on the old flow | booking |
|---|---|---|---|---|---|---|
| 1 | +1018 ms, model idle | +1495 ms | 59, at +1.5 to +59.5 s, all `1011` after 477 to 821 ms | none in 60 s | nothing | committed @8101 (+4.0 s after the call), never reported |
| 2 | +1014 ms, model idle | +1993 ms | 59, at +2.0 to +60.0 s, all `1011` after 472 to 886 ms | none in 60 s | one 28-byte segment at +0.20 s (consistent with a pong), retransmitted 8 times up to +55.8 s | committed @8056 (+4.0 s), never reported |

The attempts took 558 ms each (median over the 118). A cadence 10 times denser than round 3's changed nothing.

### Transcripts, stage 2 (verbatim, from the JSONL)

BR1 run 1, recovered in a new session with the status note:
```
2965    user audio ends ("Book me the 3pm slot tomorrow, please.")
4095    input   "Put me the 3:00 p.m. slot tomorrow, please."
4096    toolCall book_slot {"slot": "tomorrow 3pm"} id=call_242551        (generationComplete at once)
5111    blackhole: old flow dropped both ways, nothing closed
6600    client rule fires; close queued on the old socket; resume #1 -> 1011 after 550 ms
7603    resume #2 -> 1011 after 567 ms
8103    service commits BK-1001 (no session to tell)
8601, 9606   resume #3, #4 -> 1011 after 587, 557 ms
10163   window over (+3562 ms after detection); new session opened, no handle
10434   setupComplete {} on the new session (271 ms)
10438   restore sent: 1 transcript turn + system note (confirmed at 11:38:48); call_242551 left unanswered
11208   model   "Of course, your 3:00 p.m. slot for tomorrow has been successfully booked."   (4.28 s of audio)
13455   user    "Did you book it?"   (13598 interrupted: the reply was still playing)
15172   model   "Yes, your slot tomorrow at 3:00 p.m. is officially booked."
16609   the close attempt on the old socket ends: websockets close timeout; nothing was sent
```

BR2 run 1, the model cut mid-sentence by the loss:
```
4055    toolCall book_slot id=call_713725 ; 4558 user "While you do that, what should I bring to the appointment?"
9212    model   "I'm booking that slot now. Please"   (1.23 s of audio received; plays until 10441)
9426    blackhole (215 ms after the first audio chunk)
11065   service commits BK-1001
11461   client rule fires; resume #1 to #4 at +0, +1, +2, +3 s -> 1011 (581 to 609 ms)
15344   setupComplete on the new session ; 15348 restore: 3 turns (cut turn marked) + note
16506   model   "To confirm, your slot for tomorrow at 3:00 p.m. is booked. Please bring any relevant identification or documents."   (6.54 s of audio)
19044   user    "Did you book it?"   (19185 interrupted, 2.7 s into that reply's playback)
21001   model   "Yes, your 3:00 p.m. slot for tomorrow is fully booked."
```

Ablation run 1, summary only:
```
4072    toolCall book_slot id=call_812229 ; 5084 blackhole ; 6585 detection ; 8079 commit BK-1001
10402   setupComplete on the new session ; 10407 restore: 1 transcript turn + note without status
10983   toolCall book_slot {"slot": "tomorrow 3pm"} id=call_782614 -> same key as call_812229 (committed): not executed
10986   toolResponse for call_782614: {"status": "booked", "confirmation_id": "BK-1001", "duplicate_of": "call_812229", ...}
11647   model   "I'm booking the 3:00 p.m. slot tomorrow for you."
13413   user    "Did you book it?"
15196   model   "Yes, the 3:00 p.m. slot tomorrow is officially booked."
```

### Invariants over the 9 recovery runs

- The old call id was never answered: no response was sent for it on either session (9/9).
- No re-issued call when the note was sent (0/7); 2 of 2 without it, both deduplicated.
- Exactly 1 commit per run (11/11, BR0 included).
- No `toolCallCancellation`, no `goAway`, no server `error` frame.
- On the old flow, 0 client segments left after the blackhole, and the OUTPUT RST and FIN counters stayed at 0.

### What a client must do: changes from stage 2

Items 1 to 10 are unchanged. Item 11 (plan for a lockout and fall back to a new session) is now measured.

- **The fallback works as specified.** A new session restored from the client's transcript, plus a status line from the service, gave a correct answer in 9 of 9 runs, with no double booking.
- **Send the status from the service, not only the transcript.** With the note:
  - the first reply after the gap already said the booking was made (7/7);
  - the model did not re-issue the call (0/7).

  Without it, the model re-issued the call (2/2) and announced a booking "in progress" that was already done.
- **Deduplicate in the new session too.** A call re-issued after a fallback has a new id and no link to the old one; only the business key ties them. The window must cover the whole outage (600 s here, against commit_guard's 30 s).
- **The resume window costs the most and gained nothing when the old path was dead:**
  - it took 3.5 to 3.6 s of the 6.1 to 7.1 s between the loss and the first audio;
  - 0 of 36 attempts were accepted here, and 0 of 450 in round 3.

  When a close does reach the server, a resume works in about 0.5 s (rounds 2 and 3). So a window of one or two attempts loses little, if the product accepts that a loss which could have been resumed is handled by a fallback instead. Not tested: opening the new session in parallel with the attempts and keeping whichever is ready first; that would leave a second session to close.
- **Expect the model to speak as soon as the note arrives** (`turn_complete=True`; first audio 0.76 to 1.53 s after the restore). A restore that should stay silent until the user speaks would need `turn_complete=False`, which was not tested.

### Limits of stage 2

- N=4, 3, 2 and 2. One network path, one model, eight minutes on one morning. The note has one wording; another wording may behave differently. Its outro ("Continue the conversation from where it stopped") probably invited the model to redo the cut answer in BR2.
- Only the case where the old path never returns was tested. No resume was accepted in the window (0/36), so the "resumed" branch of `recovery.py` was exercised offline only.
- The commit always landed during the resume window (latency 4.0 or 7.0 s). So every note said "confirmed", and the "still being processed" line, the update note and the answer to a pending duplicate were exercised offline only.
- **The ask came before the restore reply had finished playing (9 of 9 runs).** Several facts add up:
  - the settle rule treats `generationComplete` as idle, and the model's audio arrives faster than real time;
  - so the ask started 3.0 to 3.7 s after the restore, while 0.4 to 4.0 s of that reply was still to play;
  - the server sent `interrupted` 137 to 173 ms after the ask began;
  - each reply's transcript was complete before the ask.

  The answers to "Did you book it?" are not affected. But a client that drops queued audio on `interrupted` would have played only part of each restore reply:
  - BR2: the first 2.3 to 2.7 s, that is the confirmation and at most the beginning of the regenerated "Please bring..." answer. So in this script, the regenerated answer was produced but mostly not heard.
  - BR1: 2.0 to 2.4 s of replies lasting 2.6 to 4.3 s. Where the reply ends on the key word ("...has been successfully booked."), that word would have been cut.
- The gap uses a playback model (24 kHz, back to back from arrival, flush on `interrupted`), not real audio output. In BR1 its starting point is the end of the user's request: the model says nothing between the request and the tool response even without a loss. The 2.1 to 2.2 s from the end of the request to the blackhole are part of the gap but not caused by the loss.
- Each lost session probably stays held on the server for minutes (round 3: about 8 minutes before its close). Here 11 runs in 8 minutes met no concurrency limit and no quota error, but server-side session counts were not measured.
- Not tested:
  - the note-free run without dedupe (it would double book by construction);
  - replaying speech spoken during the gap;
  - a second loss after a fallback.
- Setup: Colima was started with `--network-address --network-preferred-route` for these runs (no `limactl` process held the flow, checked with `lsof`), then stopped. Its `colima.yaml` and `lima.yaml` were restored from copies taken before the start, and the Docker context was set back to `desktop-linux`.
