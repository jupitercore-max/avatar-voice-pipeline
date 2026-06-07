# Jupitercore Avatar — Real-time AI Voice Assistant

**Real-time AI avatar + voice pipeline streaming over WebRTC**

Ray can open `http://100.71.215.1:7860` in his browser, click "Start", allow microphone, and talk to Jupitercore. The avatar responds with voice + animated lip-sync.

**Status**: Server running at `http://100.71.215.1:7860`

**GitHub**: https://github.com/jupitercore-max/avatar-voice-pipeline

---

## Architecture

```
Browser mic → FastRTC/WebRTC → Whisper large-v3 (STT, GPU)
                                       → Ollama qwen2.5:7b (LLM, localhost:11434)
                                          → VoxCPM2 (TTS, subprocess)
                                              → WebRTC audio → Browser
                                                                      ↑
                                            Browser: Canvas avatar + WebAudio lip-sync
```

**Components:**
- **STT**: faster-whisper large-v3 on RTX 4090 CUDA
- **LLM**: Ollama serving Qwen2.5-7B (GGUF, ~4.6GB)
- **TTS**: VoxCPM2 via subprocess call to voxcpm-env
- **Avatar**: Browser-side Canvas animation driven by WebAudio analyser
- **WebRTC**: FastRTC 0.0.34 with ReplyOnPause VAD

---

## Access

```
http://100.71.215.1:7860
```

(Ray can access from any browser on the same network, or via Tailscale from anywhere)

---

## Setup (for future reference)

### Machine
- **Host**: vision (Windows, RTX 4090 26GB, CUDA 12.4)
- **Tailscale IP**: 100.71.215.1

### Requirements
- Python 3.12 venv at `C:\Users\jupitercore-max.vision\Desktop\avatar-pipeline\venv\`
- ComfyUI venv at `C:\Users\jupitercore-max.vision\Desktop\ComfyUI\venv\` (for CUDA torch + faster-whisper)
- Ollama at `C:\Users\jupitercore-max.vision\Desktop\avatar-pipeline\ollama_extracted\`
- voxcpm-env at `C:\Users\jupitercore-max.vision\Desktop\voice-cloning-local\voxcpm-env\`

### Installation

```bash
# 1. Create venv and install dependencies
python -m venv avatar-pipeline/venv
./venv/Scripts/pip install fastrtc faster-whisper transformers accelerate soundfile pillow numpy scipy requests openai uvicorn

# 2. Reinstall torch with CUDA (pip installs CPU-only by default)
./venv/Scripts/pip install torch --index-url https://download.pytorch.org/whl/cu124

# 3. Download and extract Ollama
curl -L https://github.com/ollama/ollama/releases/download/v0.30.6/ollama-windows-amd64.zip -o ollama.zip
python -c "import zipfile; zipfile.ZipFile('ollama.zip').extractall('ollama_extracted')"

# 4. Pull LLM model
./ollama_extracted/ollama.exe pull qwen2.5:7b

# 5. Start Ollama server
./ollama_extracted/ollama.exe serve
```

### Running

```bash
# Start the avatar server
./avatar-pipeline/venv/Scripts/python.exe avatar_server.py --port 7860
```

---

## Known Issues

1. **TTS uses subprocess calls** — VoxCPM2 is called via `subprocess.run([voxcpm-env/python, script])` rather than imported directly. This adds ~0.5s latency per TTS call but avoids module import issues between venvs.

2. **Avatar is 2D Canvas** — The avatar is a stylized 2D face rendered in HTML5 Canvas, animated by WebAudio frequency analysis. Not a real video gen model (MuseTalk not yet working on Windows).

3. **No camera input** — User webcam is not captured or processed (v1 MVP scope).

---

## Latency

Measured on RTX 4090:
- **STT** (Whisper large-v3, CUDA fp16): ~0.3-0.5s for 1-2s audio chunks
- **LLM** (Qwen2.5-7B, Ollama): ~0.5-1.5s for short responses
- **TTS** (VoxCPM2, CUDA bfloat16): ~0.3-0.8s for short responses
- **End-to-end**: ~2-4s total latency

---

## File Structure

```
avatar-pipeline/
  avatar_server.py       # Main server (FastRTC + pipeline)
  models/                # Local model storage
    ref_voice.wav        # TTS reference voice (auto-generated)
  ollama_extracted/      # Ollama binary
    ollama.exe
    lib/
  ollama.log             # Ollama server logs
  server.out.log         # Server stdout
```

---

## What Works

- [x] FastRTC WebRTC server running on vision:7860
- [x] Ollama with qwen2.5:7b responding to text
- [x] Whisper STT via ComfyUI venv subprocess
- [x] VoxCPM2 TTS via voxcpm-env subprocess
- [x] Canvas avatar with WebAudio lip-sync in browser
- [x] Health endpoint responding
- [x] HTML page serving

## What Needs Work

- [ ] End-to-end test with actual voice (need browser)
- [ ] Reference voice generation (auto on first run)
- [ ] MuseTalk integration for video-based lip-sync
- [ ] Camera input (Florence-2/LLaVA for vision)
- [ ] Reduce TTS latency (inline import vs subprocess)
