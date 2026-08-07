# Ola Radio PTT Client

Two-way Push-to-Talk voice client for Ola Radio, reverse-engineered from the gwsdptt ARM64 binary.

## Status

| Component | Status |
|-----------|--------|
| TCP Login + Heartbeat | ✅ Working |
| Group Join | ✅ Working |
| RequestMic / ReleaseMic | ✅ Strict ACK/result validation + local talk cap |
| Voice Send (Opus → RTP → UDP) | ✅ Working |
| Voice Receive (UDP → RTP → Opus → PCM) | ✅ Built, untested with live traffic |
| UDP Heartbeat | ✅ Working |
| Multi-frame Opus decode | ✅ Implemented |
| Real-time audio playback | ✅ Implemented (sounddevice) |
| Echo suppression | ✅ Implemented |
| Push-to-talk keyboard UX | ✅ Implemented |
| TCP mic push notifications | ✅ Implemented; revocation stops UDP immediately |
| Local test suite | ✅ 69 tests passing |

## Quick Start

```bash
# Install deps
python3 -m pip install -e '.[audio]'
brew install opus

# Keep real config/token material outside version control
cp config.example.json config.json
chmod 600 config.json
# Tokens may instead be supplied with OLA_INTERNATIONAL_JWT,
# OLA_EU_ACCESS_TOKEN, OLA_PTT_TOKEN, OLA_PTT_UID, and OLA_DEVICE_ID.

# Local health/status and focused tests
ola-radio doctor
ola-radio status
ola-radio auth-sidecar status
ola-radio auth-sidecar dry-run
# Keep discovery scoped to tests/: repository-root discovery also executes
# legacy live-network experiment modules whose filenames begin with test_.
python3 -m unittest discover -s tests -v

# Run local tests (no server needed)
python3 voice_client.py loopback

# Full connectivity test (login + mic + voice start/end + listen)
python3 voice_client.py --group 16092 test

# Send a 2-second 440Hz test tone to JC Dream group
python3 voice_client.py --group 16092 tone --duration 2 --freq 440

# Send a WAV file
python3 voice_client.py --group 16092 wav recording.wav

# Listen for incoming voice (60s, with real-time speaker playback)
python3 voice_client.py --group 16092 listen --timeout 60

# Live microphone (push-to-talk — press Enter to start/stop)
python3 voice_client.py --group 16092 live

# Continuous transmit mode
python3 voice_client.py --group 16092 live --continuous

# UDP probe (registration + heartbeat + voice markers)
python3 voice_client.py --group 16092 probe
```

The complete audit and remaining production gates are in `AUDIT.md`. The exact
handoff expected from a recovered login is in `INTEGRATION_PLAN.md`.

## Official-app auth sidecar (macOS)

The sidecar borrows the official app's current session without restarting or
modifying it. It performs read-only inspection of the app container, its
Flutter preferences/cache, and credential items exposed by normal Keychain
APIs. It does **not** use packet interception, proxies, certificates, routing,
firewall rules, injection, or process attachment.

```bash
# Source availability only; always redacted
ola-radio --config config.json auth-sidecar status

# Extract + validate expiry/identity/credential shape, but write nothing
ola-radio --config config.json auth-sidecar dry-run

# Validate again, create config.json.bak.<UTC timestamp> (0600), then
# atomically replace config.json (0600)
ola-radio --config config.json auth-sidecar import
```

An import is refused when the JWT is expired, the official-app identity differs
from the configured owner, PTT identity is incomplete, or a token has an
implausible shape. Output contains only presence booleans and redacted bundle
fields. The data-protection Keychain may deny command-line access even though
the signed official app can read its items; `status` reports protected and
accessible item counts separately. In that state the sidecar exits without
changing the client config. A future helper signed for the app's access group
can satisfy the same provider without changing the CLI or `AuthBundle`.

## Files

| File | Purpose |
|------|---------|
| `voice_client.py` | **Main voice client** — two-way PTT voice, CLI, loopback tests |
| `ptt_client.py` | TCP protocol (GWPTT/HDLC framing, login, heartbeat, mic control) |
| `x_signer.py` | X-Signature generator (for REST API) |
| `rest_client.py` | REST API client + **TokenManager** (token status, refresh, login) |
| `auth_session.py` | Stable login-provider contract and normalized three-plane auth bundle |
| `config_store.py` | Atomic owner-only config writes and environment overrides |
| `official_app_sidecar.py` | Read-only official-app session provider and validated atomic import |
| `persistence.py` | SQLite contacts/groups/messages/cache cursors |
| `client.py` | Unified REST/PTT/persistence client |
| `ola_cli.py` | Unified `ola-radio` CLI |
| `test_token_manager.py` | Unit tests for TokenManager (16 tests, no network required) |
| `config.json` | Credentials and server config |
| `VOICE_BLUEPRINT.md` | Protocol reference from binary disassembly |
| `VOICE_CLIENT_LOG.md` | Build log and live test results |

## Protocol Summary

- **Codec:** Opus 16kHz mono, 20ms frames, VOIP mode (~20kbps)
- **Transport:** Custom RTP (V=3) over UDP (port 23002)
- **Control:** GWPTT over HDLC-framed TCP (port 23001)
- **Obfuscation:** XOR with nibble-swapped length byte
- **RTP Payload Types:** 0x78=voice, 0x62=heartbeat, seq 0x18E=start, 0x18F=end
- **Mic Control:** RequestMic (cmd 0x06) → ack 0x8600 → voice → ReleaseMic (cmd 0x07)

## CLI Commands

### Voice (voice_client.py)

```
loopback   — Local test suite (no server needed): Opus, XOR, RTP, HDLC, multi-frame
test       — Live server connectivity test (login, join, mic, voice start/end, listen)
tone       — Send a test tone (configurable duration/frequency)
wav        — Send a WAV file as voice
listen     — Listen for incoming voice with real-time playback
live       — Live microphone push-to-talk mode
probe      — Send UDP probes (registration, heartbeat, voice markers)
```

### REST Token Management (rest_client.py)

```
status [--live]     — Show token status (JWT expiry, EU token). --live also tests against servers
refresh-jwt          — Try JWT refresh endpoints (auth/refreshToken, user/flush-token, refresh-token)
refresh-eu           — Try EU pc-access-token refresh endpoints
login --account A  — Login to International API for fresh JWT (password prompt)
refresh-all          — Try all refresh strategies, report what worked
probe                — Probe API endpoints with current tokens
ensure [--account A --password P] — Ensure a valid JWT (check → refresh → login)
```

### Fresh-token capture

Do **not** run the archived macOS transparent-capture helper on a workstation.
It changes `pf` state globally; even with cleanup handlers, interruption can
leave unrelated services redirected. The supported acquisition boundary is a
separate Android test device/emulator or another disposable network namespace.
The Mac-side Ola process is never relaunched merely to try `SSLKEYLOGFILE` or
`CFNETWORK_DIAGNOSTICS`: this release uses Dart `SecureSocket` and does not
expose either capture mechanism.

For an Android device, use regular proxy mode (it makes no ADB or phone
configuration changes):

```bash
python3 archive/capture_traffic.py --android-proxy
# configure the phone Wi-Fi proxy to <this-mac>:8080, exercise Ola, stop capture
python3 auth_import.py --capture-dir /tmp --config config.json
python3 rest_client.py status --live
```

The Android 2.3.1 APK targets SDK 35, has no `networkSecurityConfig`, and has
no static `CertificatePinner` marker. Android therefore does not trust a
user-installed mitmproxy CA by default. A TLS failure before the addon logs an
HTTP path is a CA-trust failure unless runtime instrumentation proves a pin
check; the generic Dart `registerBadCertificateCallback` symbol alone is not
evidence of pinning. Do not weaken the logged-in phone to work around this.

The proxy may recover the REST planes, but a complete canonical artifact must
also include the PTT credential plane. The importer refuses partial, expired,
or cross-account artifacts, refuses files readable by group/other, never
prints credential values, creates an owner-only backup, and atomically saves
`config.json` mode 0600. It accepts the complete owner-only JSON artifact via
`--artifact`.

Run `python3 rest_client.py --help` for details.

## Disclaimer

Reverse-engineered from the Ola Radio app binary. All protocol details confirmed via ARM64 disassembly of `gwsdptt.framework/gwsdptt` using `otool -tV`.
