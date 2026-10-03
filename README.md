# gemini-live-resume-test

License: MIT.

A test series on the Gemini Live API (`gemini-3.8-live`, Gemini Developer API with an
API key, `google-genai` 2.25.0 with websockets 16.1.1) that measures what happens to a
voice session when its WebSocket connection is lost while a side-effecting tool call is
pending, and what session resumption can and cannot recover.

The setup is a voice booking assistant. The user says "Book me the 3pm slot tomorrow,
please.", the model calls `book_slot`, a fake backend takes 4 s (7 s in some scenarios)
to commit, and the connection is lost before the result reaches the model. Then the
client tries to resume the session with its resumption handle, sends the result, and
asks "Did you book it?". The answer is compared with what the backend actually did.

43 sessions over two days (2026-10-02 evening, 2026-10-03 morning), in four rounds:

| round | how the connection is lost | sessions |
|---|---|---|
| 1 | the client aborts its own TCP connection, so the server sees it close | 17 |
| 2 | an application-level freeze: a local proxy stops forwarding bytes and closes nothing | 9 |
| 3 | real packet loss: iptables drops the flow both ways in a Linux container, nothing is closed | 6 |
| stage 2 | round 3's packet loss, with a client-side recovery (`recovery.py`) | 11 |

[FINDINGS.md](FINDINGS.md) is the detailed record: method, every table, verbatim
transcripts, and the limits of each round. This page is the short version.

## Findings

### 1. A pending tool call survives a resume that works

In all 14 runs that had a pending call and a successful resume, the resumed session
accepted the `FunctionResponse` for the call id issued before the drop. The server never
re-issued the call, never sent `toolCallCancellation`, and returned no error. There was
exactly one booking per run. The call stayed answerable for as long as the session could
be resumed: 154 s after it was issued in round 2, 83.6 s in round 3.

If the client does not send that response after the resume, the model stays at "in
progress". In 3 of 3 such runs it answered "Did you book it?" with an in-progress
sentence ("I'm booking the 3 pm slot for you now." in run 1), 2.9 to 3.1 s after the
booking had committed. The model only knows what the last tool response told it.

### 2. The immediate resume after an idle drop is refused, and works about 1.5 s later

When the connection dropped while the model was idle (round 1, the server saw the close):

- a resume sent at once was closed with `1011 Internal error encountered.` in 12 of 12
  runs, 472 to 704 ms after it started;
- a resume about 1.5 s after the drop worked in 12 of 12 (a retry 1 s after the refusal
  in 11 runs, a single attempt at 1.6 s in 1);
- a clean WebSocket close instead of an abort changed nothing (1 of 1);
- when the model was speaking at the drop, the immediate attempt worked (3 of 3).

From the drop to the resumed `setupComplete`: 2.0 to 2.3 s after an idle drop (n=12),
0.5 to 0.6 s after a drop during speech (n=3). Each connection received exactly one
`sessionResumptionUpdate`, right after `setupComplete`, and none later. The handle issued
before the user spoke still restored everything up to the drop.

### 3. After a silent loss, every resume is refused while the server holds the old connection

A silent loss is what a phone sees when it drops off Wi-Fi: packets stop, and no close
frame, FIN or RST reaches the server. The client detected it with its own pings (one
every 0.5 s, lost after 2 s without a frame or a pong), 1.5 to 2.0 s after the loss.

- **Application-level freeze (round 2).** 40 of 40 resume attempts, from 1.8 s to 133 s
  after the loss, were refused with the same `1011`. In a long probe (N=1), nothing worked
  for 12.8 minutes; the server closed the frozen connection with `1011` after about 8
  minutes, and the handle did not work after that either.
- **Real packet loss (round 3).** 450 of 450 attempts, made at detection and then every
  10 s, were refused with `1011` for as long as I tried: 15 minutes (last attempt at
  +892 s), in 5 of 5 runs. None hung. With the model idle, the server sent nothing at all
  for 479 s: no WebSocket ping, no TCP keepalive. With unacknowledged data in flight, its
  TCP gave up retransmitting after about 2 minutes, and the session stayed locked anyway.
- **Closing the old connection unlocks it at once.** Once the path came back and the
  client closed the old connection (close 1000, echoed by the server), the next resume
  worked 0.5 to 0.6 s later (6 of 6 across rounds 2 and 3). The path coming back was not
  enough (N=1): with the old connection open again and carrying traffic, attempts were
  still refused until the client closed it.

In every silent-loss run the booking committed 1 to 3 s after the loss. In the runs where
the old path never came back, the user was never told.

### Client-side recovery (stage 2)

`recovery.py` is a reference pattern for the case above. On a detected loss it tries to
resume for 4 s (an attempt at detection, then one per second), sends a close on the old
socket in parallel, and if no resume is accepted it opens a new session without a handle.
It then restores the conversation with one `send_client_content`: the last turns of the
client's own transcript, plus a "System note" with one status line per side effect, taken
from the backend, not from the model. The old call id is never answered. A call the model
issues again is deduplicated by business key and answered from the existing job.

- 9 of 9 recovered runs answered "Did you book it?" correctly, with one booking each. The
  resume window never helped here: 36 of 36 attempts were refused.
- With the status note, the model's first sentence already said the booking was made
  ("Of course, your 3:00 p.m. slot for tomorrow has been successfully booked.") and it
  never re-issued the call (0 of 7). Without it (N=2), the model re-issued `book_slot` in
  2 of 2 runs; the dedupe kept that from becoming a second booking, but its first sentence
  still announced a booking in progress ("I'm booking the 3:00 p.m. slot tomorrow for
  you.").
- From the loss to the first audio of the recovered session: 6.1 to 7.1 s. That is
  detection 1.5 to 2.0 s, the resume window 3.55 to 3.6 s (the largest share), the new
  session's setup 0.25 to 0.32 s, and the first audio 0.76 to 1.2 s after the restore.
- A plain resume loop without the fallback (one attempt per second for 60 s, N=2) got 118
  refusals out of 118 and never told the user.
- The close sent on the old socket at detection never left the client: on a dead path it
  costs nothing and does nothing.

## What a client must do

These follow from what I measured; they are not guarantees from the API.

1. Keep your own ledger of tool calls by call id, across connections. After a resume,
   send the result for the old id, even if the side effect finished during the gap.
2. Retry a refused resume. A `1011` right after an idle drop is not a dead handle; wait
   about 1.5 s or retry after 1 s. Keep the handle you have: only one arrives per
   connection, and it restores the latest state.
3. Detect the loss yourself with WebSocket pings. An idle healthy connection carries no
   server frames for many seconds, and the server never pinged the client.
4. Get a close to the server when you can. If the path comes back, close the old
   connection before resuming; that is what unlocks the session. A close queued on a dead
   path leaves only when the client kernel next retransmits or probes.
5. Put a short cut-off on resuming and fall back to a new session. After a silent loss
   on a path that does not come back, the session stayed locked for the full 15 minutes I
   tried, and retrying gained nothing in over 500 attempts.
6. In the new session, restore the status of each side effect from your backend, not
   only the transcript. Without it the model re-issued the call and announced a booking
   that was already done.
7. Deduplicate side effects by business key, in the new session too. A re-issued call
   has a new id, and only the business key ties it to the old one.
8. Do not count on transparent mode or `lastConsumedClientMessageIndex` on the Gemini
   Developer API: the SDK refuses `transparent` in this mode, and the server closes the
   setup with `1007` when it is sent anyway. Buffer anything you need to replay yourself.
9. Do not wait for `goAway`. None arrived in any session, including a frozen connection
   held for 12.8 minutes, which the server ended with a `1011` close instead.
10. Expect speech cut by a drop to be partly repeated after a resume.

## Minimal reproduction

`repro_resume_lockout.py` (179 lines, google-genai 2.25.0 and python-dotenv only) shows
finding 3 on a laptop, without Docker or root. It embeds a small asyncio HTTP CONNECT
proxy that can freeze its tunnel (stop forwarding both ways without closing anything),
routes only the first connection through it, and connects the resume attempts directly.
It opens a session with session resumption on, keeps the handle from the first
`SessionResumptionUpdate` (never printed), sends one text turn, freezes the tunnel, then
tries to resume every 5 s for `--seconds` (default 60) and prints one line per attempt.
As a control it unfreezes the tunnel, closes the first connection cleanly so a close
frame reaches the server, waits 2 s, and tries once more.

```sh
cp .env.example .env                        # then set GEMINI_API_KEY=... in .env
uv run repro_resume_lockout.py --seconds 60
```

Runs on 2026-10-03 on macOS, raw stdout in `results/repro/`:

| output | attempts refused while frozen | refusal | control |
|---|---|---|---|
| [lockout_run1.txt](results/repro/lockout_run1.txt) | 12 of 12, +5 to +60 s | close `1011` "Internal error encountered.", 510 to 676 ms each | server echoed close 1000; resume `setupComplete` after 582 ms |
| [lockout_run2.txt](results/repro/lockout_run2.txt) | 12 of 12, +5 to +60 s | close `1011` "Internal error encountered.", 497 to 665 ms each | server echoed close 1000; resume `setupComplete` after 543 ms |

The first attempt comes 5 s after the freeze so that it cannot be confused with finding 2
(the refusal right after an idle drop, which clears in about 1.5 s).

## Reproduce

Requires [uv](https://docs.astral.sh/uv/); the project pins Python 3.13. Put your key in
`.env` (`cp .env.example .env`). The harness reads it only from `GEMINI_API_KEY`, never
prints it, and redacts error strings before logging. The live commands below write to
`out/` directories, so the published `results/` stay untouched and the harness's session
budget (`--budget`, default 18, counted in `<results-dir>/sessions.log`) starts from zero.

Offline checks, no key and no network beyond 127.0.0.1:

```sh
uv run introspect.py                                  # SDK surface for session resumption
uv run test_freeze_proxy.py; uv run test_recovery.py  # round 2 proxy, stage 2 recovery logic
uv run make_summary.py                                # rebuild results/summary.md from the JSONL
```

Round 1, the client aborts its connection:

```sh
uv run resume_test.py --scenario R0 -n 1 --results-dir out/r1
for s in R1 R2 R3 R4; do uv run resume_test.py --scenario $s -n 3 --results-dir out/r1; done
uv run resume_test.py --scenario R1 -n 1 --drop-mode clean --name P2_R1_clean_close --results-dir out/r1
```

Round 2, application-level freeze through `freeze_proxy.py` (its TCP state probe uses a
macOS socket option and records nothing elsewhere):

```sh
uv run resume_test.py --scenario N1 -n 3 --reconnect-retries 7 --results-dir out/r2
uv run resume_test.py --scenario N2 -n 3 --results-dir out/r2; uv run resume_test.py --scenario N3 -n 2 --results-dir out/r2
uv run resume_test.py --scenario N1 -n 1 --retry-fixed 60 --reconnect-retries 12 --name P4_N1_long_freeze --results-dir out/r2
```

Round 3, real packet loss, in a Linux container with `NET_ADMIN` (each B1 and B2 run
lasts 15 minutes):

```sh
mkdir -p out/r3 && colima start --network-address --network-preferred-route && docker build -t resume-test .
R="docker run --rm --cap-add NET_ADMIN -v $PWD/.env:/app/.env:ro -v $PWD/out/r3:/app/results resume-test python resume_test.py"
$R --scenario B1 -n 3; $R --scenario B2 -n 2 --disconnect-after 0.2; $R --scenario B3 -n 1
```

Why the Colima flags: with Colima's default network, the VM reaches the Internet through
Lima's user-mode network stack, and `lsof` on the Mac showed the TCP connection to Google
owned by `limactl`. A process on the Mac then terminates Google's TCP and keeps ACKing, so
the server would never see a dead peer. `--network-address --network-preferred-route`
routes the VM through Apple's NAT (vzNAT) instead; it needs no root. With it, no Mac
process held the connection, and only packet-level NATs sat between the container and
Google. The `.env` is mounted read-only and never copied into the image. In the series,
B2 run 1 dropped 1.0 s after the first model audio chunk, after the whole reply had
already arrived; the command above uses run 2's 0.2 s, inside the audio burst. I ran
round 3 only on macOS with Colima 0.9.1, not on a Linux host.

Stage 2, client-side recovery (same container and `$R`):

```sh
$R --scenario BR1 -n 4; $R --scenario BR2 -n 3
$R --scenario BR1 -n 2 --no-status-note --name BR1_nonote
$R --scenario B1 -n 2 --bh-period 1 --bh-max 60 --name BR0
```

Each run appends its events to `<results-dir>/<name>.jsonl` and one row per run to
`summary.md` there. FINDINGS.md lists the exact invocations used for the published
results, including the offline container check of the blackhole.

## Results and logs

- `results/<name>.jsonl`: every scripted action and every server event, in ms since the
  session start, one file per scenario. Each run starts with a `run_start` event (its
  configuration) and ends with a `run_end` event (its summary row). These files are the
  unedited harness output. No resumption handle is stored anywhere: only its length and
  a 10-character SHA-256 prefix.
- `results/summary.md`: generated from the JSONL by `make_summary.py`, with
  `resume_test.py`'s own formatting code. One value differs from the row stored at run
  time: the BR2 run 1 gap is 6.06 s, not 6.54 s, because that run's harness version took
  its snapshot of what the user had heard at the loss instead of at detection, and missed
  an audio chunk read 7 ms later. The generator recomputes the gap from the audio events
  and says so under the table.
- `results/sessions.log`: one line per harness session, the budget ledger.
- `results/repro/`: stdout of the minimal reproduction.
- `results/figures/lockout.png`: the lockout (B1, B2) and the recovery (BR1) in one figure,
  drawn from the JSONL by `make_figure.py`.
- `results/clip/`: `lockout-recovery.mp4` (and a GIF), B1 run 1 then one BR1 session, drawn
  by `make_clip.py`. That BR1 session was recorded on 2026-10-03 for the clip only, with
  `--save-audio` (the model's output audio and its chunk times are in `results/clip/audio_out/`).
  It is not one of the 43 sessions above, and its JSONL, ledger and summary stay in
  `results/clip/`. It behaved like the four BR1 runs: 4 refusals, a new session 3.91 s after
  detection, first model audio 6.46 s after the loss, one booking, a correct answer.

```sh
uv run --with matplotlib python make_figure.py
uv run --with imageio-ffmpeg --with pillow --with numpy python make_clip.py
# the clip session (round 3 container; results/clip mounted as the results dir):
docker run --rm --cap-add NET_ADMIN -e TZ=Europe/Paris -v $PWD/.env:/app/.env:ro \
  -v $PWD/results/clip:/app/results resume-test python resume_test.py --scenario BR1 -n 1 \
  --save-audio --name BR1_audio --budget 1
```

## Limits

- Small N: 1 to 4 runs per scenario. One network path, one model, the Gemini Developer API
  only (not Vertex AI, now Gemini Enterprise Agent Platform), two days.
- The speech is synthetic (macOS `say`, one voice, digital silence between utterances),
  and the booking backend is a fake in-process job.
- Round 2's freeze is application-level: the proxy's kernel still ACKs and answers TCP
  probes. Round 3 removes that caveat with real packet loss, but through Colima's vzNAT
  and three packet-level NATs, on one home connection.
- In round 3 the client drops its own outgoing packets with iptables, so its socket runs
  the probe timer instead of the retransmission timer. The server side sees a dead peer
  either way.
- The server's 53-byte segment at +479 s is read as a close frame from its size and
  timing, which match the close decoded in round 2. TLS was not decrypted.
- Stage 2 used one wording of the status note. The commit always landed inside the resume
  window, so the "still being processed" path was tested offline only. In 9 of 9 runs the
  question came before the restore reply had finished playing; the answers are not
  affected, but a client that drops queued audio on `interrupted` would have cut part of
  that reply. The gap uses a playback model (24 kHz, back to back from arrival), not real
  audio output.
- Not tested: replaying speech spoken during the gap, a second loss after a fallback,
  opening the new session in parallel with the resume attempts, a path that comes back
  after the server's own close, and how many lost sessions the server holds at once.

## Documentation used (read 2026-10-02)

- Gemini API, [Session management with Live API](https://ai.google.dev/gemini-api/docs/live-api/session-management)
- [Live API, WebSockets API reference](https://ai.google.dev/api/live)
- [Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)
- [Gemini 3.8 Live model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live)
- [Tool use with Live API](https://ai.google.dev/gemini-api/docs/live-api/tools)
- Google Cloud, [Start and manage live sessions](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/start-manage-session)
- Google Cloud, [Best practices with Gemini Live API](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/best-practices)
- Google Cloud, [Gemini Live API reference](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/multimodal-live)

None of these pages says what happens to a tool call pending at a disconnect, how soon a
resume can succeed, or which errors a resume can return. FINDINGS.md, Part 1, has what
they do say.

## Files

- `resume_test.py`: the harness (one session per run, every scenario above).
- `recovery.py`: the client-side recovery pattern used in stage 2.
- `freeze_proxy.py`: the freezable CONNECT proxy of round 2.
- `blackhole.py`: the iptables blackhole and packet watch of round 3.
- `repro_resume_lockout.py`: the minimal reproduction.
- `make_summary.py`: rebuilds `results/summary.md` from the JSONL.
- `make_figure.py`, `make_clip.py`: the figure and the demo clip, from the JSONL.
- `test_freeze_proxy.py`, `test_blackhole.py`, `test_recovery.py`: offline checks.
- `introspect.py`: prints the SDK surface used (no network, no key).
- `Dockerfile`: the round 3 and stage 2 client container.
- `assets/audio/`: the four speech clips, 16 kHz 16-bit mono (macOS `say`, voice
  Samantha).
- `.env.example`: the `GEMINI_API_KEY=` placeholder; `.env` is git-ignored.

An AI coding agent wrote the harness under my direction; the setup and the claims were
reviewed before publication.
