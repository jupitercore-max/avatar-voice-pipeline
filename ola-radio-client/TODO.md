# Ola Radio Client — TODO

## ✅ Completed

- [x] Full API reverse-engineered (auth flow, REST endpoints, PTT protocol)
- [x] **X-Signature CRACKED** (Jul 9, 2026) — `x_signer.py` working, verified against 4 samples
- [x] AES decryption of embedded secret
- [x] Blutter decompilation of Dart AOT binary
- [x] **X-Signature integrated into `rest_client.py`** (Jul 9, 2026)
- [x] **EU REST API client** (`EuRestApiClient`) for euweb2c:7002 endpoints
- [x] **Client.py updated** to wire both API clients together
- [x] **Traffic capture script** (`capture_traffic.py`) for getting fresh JWT
- [x] **Live API test script** (`test_live_api.py`) with self-test + endpoint tests
- [x] Local SQLite DB access (read messages, audio blobs)
- [x] Audio extraction + Whisper transcription pipeline
- [x] CLI interface (`cli.py`)
- [x] **PTT TCP protocol client** (HDLC framing, login, heartbeat, join group) — ✅ Live tested
- [x] **Voice client built from disassembly** (Jul 10, 2026)
  - Opus 16kHz mono 20ms frames, VOIP mode, ~20kbps
  - Custom RTP V=3 over UDP port 23002
  - XOR nibble-swap obfuscation
  - RequestMic/ReleaseMic (cmd 0x06/0x07, ack 0x8600)
  - Voice start/end markers (seq 0x18E/0x18F)
  - UDP heartbeat (PT=0x62)
- [x] **Live server test passed** (Jul 10, 2026)
  - TCP login → JoinGroup → RequestMic granted (0x8600) → voice frames sent → ReleaseMic
  - 50-100 Opus frames transmitted successfully
- [x] **Multi-frame Opus decoding** (Jul 10, 2026) — handles length-prefixed sub-frames
- [x] **Real-time audio playback** (Jul 10, 2026) — sounddevice RawOutputStream in receive loop
- [x] **Echo suppression** (Jul 10, 2026) — skips own voice packets during transmit
- [x] **Push-to-talk keyboard UX** (Jul 10, 2026) — Enter to start/stop, background input thread
- [x] **TCP mic push notifications** (Jul 10, 2026) — handles mic grant/release from other users
- [x] **Local loopback test suite** (Jul 10, 2026) — 6 tests: Opus, XOR, RTP, markers, HDLC, multi-frame
- [x] **JoinGroup false-negative fixed** (Jul 10, 2026) — server accepts silently, now returns success
- [x] **Mic push command constants** (Jul 10, 2026) — CMD_REQUEST_MIC_ACK (0x8600), CMD_RELEASE_MIC_ACK (0x8700)
- [x] **Receive-path hardening** (Jul 27, 2026) — RTP length validation,
  jitter/reordering, duplicate rejection, Opus FEC/PLC, and missing-END flush
- [x] **Unified TCP reader** (Jul 27, 2026) — one socket reader with correlated
  ACK waiters and independent push-event dispatch
- [x] **Strict floor control** (Jul 27, 2026) — ACK result validation, denial
  handling, immediate stop on LostMic/MemberGetMic, and a 60-second local cap
- [x] **Delivery pipeline offline repair** (Jul 27, 2026) — router integration,
  deferred checkpoints, idempotency, and side-effect-free dry-run

## 🔲 Blocked: Expired Auth Tokens (REST API only — PTT voice works)

**Current blocker:** The JWT (1-hour expiry) and pc-access-token are both expired.
This only affects REST API calls. **PTT voice uses a separate token that still works.**

**Proof x-signature formula IS correct:**
- Self-test passes 4/4 captured samples
- Server accepts fresh x-signature with OLD captured timestamp (returns 10004 = time sync)

**To unblock REST API:**
1. Run `sudo python3 capture_traffic.py` then restart the Ola Radio app
2. Update config.json with the fresh JWT from `/tmp/ola-fresh-jwt.txt`

## 🔲 Remaining (human partner needed)

- [ ] **Human voice test** — Ray listens on JC Dream group (16092) while we transmit a tone
- [ ] **Receive voice test** — Ray transmits, we decode and play through speaker
- [ ] **Two-way live test** — Ray and OpenClaw alternate transmitting and receiving
- [ ] **JWT auto-refresh** — Investigated Jul 10, 2026. **Cannot be fully automated.** Both token systems have a chicken-and-egg bootstrap: International API login (`/api/login`) requires existing valid JWT (returns 10005 without auth); EU API login requires unknown bootstrap `token` header (returns 1001). Refresh endpoints (`/user/flush-token`, `/auth/refreshToken`, `/refresh-token`) all reject expired tokens. TokenManager implemented in `rest_client.py` with `status`, `refresh-jwt`, `refresh-eu`, `login`, `refresh-all`, `ensure` commands — will auto-refresh when tokens are still valid, gives clear manual capture instructions when expired.
- [ ] **Voice receive verification** — Confirm multi-frame decode works with real traffic
- [ ] **Live contention verification** — confirm inferred MemberGetMic/LostMic
  command IDs and denial result codes against real official-app traffic
