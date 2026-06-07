#!/usr/bin/env python3
"""
Jupitercore - Real-time AI Avatar via WebRTC
Run on vision (Windows + RTX 4090) via: python avatar_server.py

Usage:
  python avatar_server.py [--port 7860]

Access: http://100.71.215.1:7860

Architecture:
  Browser mic → FastRTC/WebRTC → Whisper (STT, GPU)
                                   → Ollama qwen2.5:7b (LLM, localhost:11434)
                                      → TTS (VoxCPM2 via subprocess)
                                          → WebRTC audio → Browser
                                                              ↑
                                        Browser: Canvas avatar (WebAudio lip-sync)
"""

import os, sys, io, time, argparse, wave, uuid, subprocess, tempfile, requests
import numpy as np, torch
from pathlib import Path

# ─── PATHS ───────────────────────────────────────────────────────────────────
BASE_DIR   = Path(os.environ.get("AVATAR_BASE_DIR",
    r"C:\Users\jupitercore-max.vision\Desktop\avatar-pipeline"))
MODELS_DIR = BASE_DIR / "models"
MODELS_DIR.mkdir(exist_ok=True)
OLLAMA_EXE = BASE_DIR / "ollama_extracted" / "ollama.exe"
VOXCPM_PY  = Path(r"C:\Users\jupitercore-max.vision\Desktop\voice-cloning-local\voxcpm-env\Scripts\python.exe")
VOXCPM_REF = Path(r"C:\Users\jupitercore-max.vision\Desktop\voice-cloning-local\reference-audio")
REF_VOICE  = MODELS_DIR / "ref_voice.wav"
COMFYUI_VENV_PY = Path(r"C:\Users\jupitercore-max.vision\Desktop\ComfyUI\venv\Scripts\python.exe")
PORT         = int(os.environ.get("AVATAR_PORT", "7860"))
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# ─── DEVICE ───────────────────────────────────────────────────────────────────
# Use ComfyUI venv for CUDA support (avatar-pipeline venv has CPU-only torch)
def get_torch():
    try:
        # Try ComfyUI venv first (has CUDA torch)
        sys.path.insert(0, str(COMFYUI_VENV_PY.parent.parent / "Lib" / "site-packages"))
        import torch as _torch
        if _torch.cuda.is_available():
            return _torch, "cuda", _torch.cuda.get_device_name(0)
    except: pass
    # Fall back to avatar-pipeline venv
    import torch as _torch
    if _torch.cuda.is_available():
        return _torch, "cuda", _torch.cuda.get_device_name(0)
    return _torch, "cpu", "CPU"

_torch, DEVICE, GPU_NAME = get_torch()
VRAM_GB = _torch.cuda.get_device_properties(0).total_memory / 1e9 if DEVICE == "cuda" else 0
print(f"[SYS] {DEVICE} | {GPU_NAME} | {VRAM_GB:.0f}GB VRAM")

# ─── FASTRTC ──────────────────────────────────────────────────────────────────
print("[INIT] Importing FastRTC...")
from fastrtc import Stream, ReplyOnPause, AdditionalOutputs
print("[INIT] FastRTC imported OK")

# ─── STT ─────────────────────────────────────────────────────────────────────
print("[INIT] Loading Whisper (STT) on ComfyUI venv...")
_stt_model = None

def load_whisper():
    global _stt_model
    if _stt_model is None:
        # Import faster_whisper using ComfyUI venv Python for CUDA support
        # We need to run it in the ComfyUI venv
        print("[INIT] Whisper will be loaded on first use via ComfyUI venv")
    return _stt_model

def transcribe(audio_np: np.ndarray, sr: int) -> str:
    """Transcribe using faster-whisper via ComfyUI venv subprocess."""
    # Write audio to temp file
    tmp_wav = BASE_DIR / f"_stt_{uuid.uuid4().hex[:8]}.wav"
    with wave.open(str(tmp_wav), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((audio_np * 32767).astype(np.int16).tobytes())

    try:
        result = subprocess.run(
            [str(COMFYUI_VENV_PY), "-c", f"""
import sys, faster_whisper, numpy as np, wave
sys.path.insert(0, r'{COMFYUI_VENV_PY.parent.parent}\\Lib\\site-packages')
model = faster_whisper.WhisperModel('large-v3', device='cuda', compute_type='float16')
with wave.open(r'{tmp_wav}', 'rb') as wf:
    data = wf.readframes(wf.getnframes())
    audio = np.frombuffer(data, np.int16).astype(np.float32) / 32768.0
segs, _ = model.transcribe(audio, language='en', beam_size=5, vad_filter=True,
    vad_parameters={{'min_silence_duration_ms': 400}})
text = ' '.join(s.text.strip() for s in segs if s.text.strip())
print(repr(text))
"""],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PATH": os.environ.get("PATH","") + f";{COMFYUI_VENV_PY.parent.parent}"}
        )
        tmp_wav.unlink(missing_ok=True)
        if result.returncode == 0 and result.stdout:
            import ast
            try: return ast.literal_eval(result.stdout.strip().split('\n')[-1])
            except: return result.stdout.strip().split('\n')[-1].strip("'\"")
        else:
            print(f"[STT ERR] {result.stderr[:200]}")
    except Exception as e:
        tmp_wav.unlink(missing_ok=True)
        print(f"[STT ERR] {e}")
    return ""

# ─── LLM ─────────────────────────────────────────────────────────────────────
_system_prompt = """You are Jupitercore, a friendly AI assistant on a Mac mini with a powerful GPU.
Keep responses SHORT (1-3 sentences) for real-time voice. Be helpful and playful.
Respond as if speaking aloud - natural speech only."""

_history = []

def llm_reply(text: str) -> str:
    global _history
    msgs = [{"role":"system","content":_system_prompt}]
    for h in _history[-10:]: msgs.append(h)
    msgs.append({"role":"user","content":text})
    try:
        r = requests.post("http://localhost:11434/api/chat",
            json={"model": OLLAMA_MODEL, "messages": msgs, "stream": True,
                  "options": {"num_predict": 256, "temperature": 0.8}},
            stream=True, timeout=120)
        full = ""
        for line in r.iter_lines():
            if line:
                try: full += __import__('json').loads(line)["message"]["content"]
                except: pass
        return full or "I'm not sure how to respond."
    except Exception as e:
        print(f"[LLM ERR] {e}"); return "Sorry, I couldn't generate a response."

# ─── TTS ─────────────────────────────────────────────────────────────────────
def synthesize(text: str) -> tuple[np.ndarray, int]:
    """Generate speech using VoxCPM2 via voxcpm-env subprocess."""
    # Write text to temp file
    tmp_out = BASE_DIR / f"_tts_{uuid.uuid4().hex[:8]}.wav"
    tmp_script = BASE_DIR / f"_tts_script_{uuid.uuid4().hex[:8]}.py"

    gen_script = f"""
import sys, torch, torchaudio
sys.path.insert(0, r'{VOXCPM_PY.parent.parent}')
from voxcpm import VoxCPM
model = VoxCPM.from_pretrained()
model = model.to(dtype=torch.bfloat16, device='cuda')
text = {repr(text)}
# Use built-in reference or generate without
out = model.generate(text=text, ref_audio=None, ref_sr=24000)
torchaudio.save(r'{tmp_out}', out.cpu(), 24000)
print('OK')
"""
    tmp_script.write_text(gen_script)

    try:
        result = subprocess.run(
            [str(VOXCPM_PY), str(tmp_script)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ}
        )
        tmp_script.unlink(missing_ok=True)

        if tmp_out.exists():
            with wave.open(str(tmp_out), 'rb') as wf:
                sr = wf.getframerate()
                data = wf.readframes(wf.getnframes())
                wav = np.frombuffer(data, np.int16).astype(np.float32) / 32768.0
            tmp_out.unlink(missing_ok=True)
            return wav.astype(np.float32), sr
        else:
            print(f"[TTS ERR] No output: {result.stdout[:100]} {result.stderr[:100]}")
    except Exception as e:
        tmp_script.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)
        print(f"[TTS ERR] {e}")

    return np.zeros(16000, dtype=np.float32), 16000

# ─── HANDLER ─────────────────────────────────────────────────────────────────
async def avatar_handler(audio_info):
    """Async generator: receive audio from browser, yield audio response."""
    # audio_info is (sample_rate, audio_array) from ReplyOnPause
    if isinstance(audio_info, tuple) and len(audio_info) == 2:
        sr = audio_info[0]
        audio_np = audio_info[1]
    else:
        audio_np = np.array(audio_info)
        sr = 16000

    if hasattr(audio_np, 'to_ndarray'):
        audio_np = audio_np.to_ndarray()

    t0 = time.time()
    user_text = transcribe(audio_np, sr)
    stt_t = time.time() - t0

    print(f"\n[STT] {stt_t:.1f}s | '{user_text[:70]}'" if user_text else f"\n[STT] {stt_t:.1f}s | (silence)")

    if not user_text.strip():
        yield (16000, np.zeros(8000, dtype=np.float32))
        return

    t1 = time.time()
    response = llm_reply(user_text)
    llm_t = time.time() - t1
    print(f"[LLM] {llm_t:.1f}s | '{response[:60]}'")

    _history.append({"role":"user","content":user_text})
    _history.append({"role":"assistant","content":response})
    if len(_history) > 20: _history[:] = _history[-20:]

    t2 = time.time()
    wav, tts_sr = synthesize(response)
    tts_t = time.time() - t2
    total = time.time() - t0
    print(f"[TTS] {tts_t:.1f}s ({tts_sr}Hz, {len(wav)/tts_sr:.1f}s) | TOTAL: {total:.1f}s")

    yield (tts_sr, wav.astype(np.float32))

# ─── HTML ─────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width">
<title>Jupitercore Avatar</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #06060e; color: #b0b0d0;
    font-family: 'Segoe UI', system-ui, sans-serif;
    display: flex; flex-direction: column; align-items: center;
    min-height: 100vh; padding: 24px 16px;
  }
  h1 { color: #7070ff; font-size: 1.5rem; letter-spacing: 0.06em;
       text-shadow: 0 0 24px rgba(100,100,255,0.35); margin-bottom: 4px; }
  #sub { color: #404060; font-size: 0.78rem; margin-bottom: 16px; }
  #avatar-wrap {
    position: relative; width: 360px; height: 360px; border-radius: 50%;
    background: radial-gradient(ellipse at 50% 38%, #121228 0%, #06061a 70%);
    box-shadow: 0 0 70px rgba(50,50,180,0.12), 0 0 140px rgba(30,30,120,0.06);
    margin-bottom: 14px; overflow: hidden;
  }
  canvas { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
  #level-ring {
    position: absolute; bottom: -7px; left: 50%; transform: translateX(-50%);
    width: 90px; height: 4px; border-radius: 2px; background: #0a0a18; overflow: hidden;
  }
  #level-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #3030ee, #6060ff); transition: width 0.04s linear; }
  #transcript { max-width: 500px; text-align: center; font-size: 0.82rem; color: #6060a0; margin-bottom: 14px; min-height: 18px; }
  #micBtn {
    background: linear-gradient(135deg, #1a1a90, #2828b0); color: #d0d0ff;
    border: 1px solid #3030cc; padding: 11px 30px; border-radius: 22px;
    cursor: pointer; font-size: 0.95rem; font-weight: 500;
    transition: all 0.2s; box-shadow: 0 3px 18px rgba(50,50,200,0.18);
  }
  #micBtn:hover { background: linear-gradient(135deg, #2222a0, #3030c0); box-shadow: 0 3px 24px rgba(70,70,255,0.28); }
  #micBtn.active { background: linear-gradient(135deg, #2828a0, #3838c8); box-shadow: 0 3px 28px rgba(90,90,255,0.35); }
  .hint { font-size: 0.7rem; color: #353555; margin-top: 7px; }
  #latency { position: fixed; top: 10px; right: 12px; font-size: 0.68rem; color: #252540; font-family: monospace; }
</style>
</head>
<body>
<div id="latency"></div>
<h1>🎙️ Jupitercore</h1>
<div id="sub">Real-time AI voice assistant</div>
<div id="avatar-wrap">
  <canvas id="avatar" width="360" height="360"></canvas>
  <div id="level-ring"><div id="level-bar"></div></div>
</div>
<div id="transcript">Click Start to begin</div>
<button id="micBtn">🎤 Start</button>
<div class="hint">Allow microphone when prompted</div>
<script>
const canvas = document.getElementById('avatar');
const ctx = canvas.getContext('2d');
const W = 360, H = 360;
const cx = W/2, cy = H/2 - 8, r = 82;
const eyeY = cy - 26, mouthY = cy + 52;
let mouth = 0, tMouth = 0, blinkT = 0, blinkA = 0, aLvl = 0, tLvl = 0, talking = false;

function draw() {
  ctx.clearRect(0,0,W,H);
  const g = ctx.createRadialGradient(cx-16, cy-16, 3, cx, cy, r);
  g.addColorStop(0,'#5252c0'); g.addColorStop(0.5,'#3434a0'); g.addColorStop(1,'#141490');
  ctx.beginPath(); ctx.arc(cx,cy,r,0,Math.PI*2);
  ctx.fillStyle=g; ctx.fill();
  ctx.strokeStyle='rgba(90,90,220,0.22)'; ctx.lineWidth=2; ctx.stroke();
  const eyeH = Math.max(2, 10 - blinkA*8);
  ctx.fillStyle='#e0e0ff';
  ctx.beginPath(); ctx.ellipse(cx-32,eyeY,8.5,eyeH,0,0,Math.PI*2); ctx.fill();
  ctx.beginPath(); ctx.ellipse(cx+32,eyeY,8.5,eyeH,0,0,Math.PI*2); ctx.fill();
  ctx.fillStyle='#080830';
  ctx.beginPath(); ctx.arc(cx-32,eyeY+1,4,0,Math.PI*2); ctx.fill();
  ctx.beginPath(); ctx.arc(cx+32,eyeY+1,4,0,Math.PI*2); ctx.fill();
  ctx.fillStyle='rgba(255,255,255,0.6)';
  ctx.beginPath(); ctx.arc(cx-33,eyeY-1.5,1.6,0,Math.PI*2); ctx.fill();
  ctx.beginPath(); ctx.arc(cx+31,eyeY-1.5,1.6,0,Math.PI*2); ctx.fill();
  const mW=40, mH=Math.max(3,tMouth*32);
  ctx.fillStyle='#030318';
  ctx.beginPath(); ctx.ellipse(cx,mouthY,mW/2,mH/2,0,0,Math.PI*2); ctx.fill();
  ctx.strokeStyle='rgba(70,70,190,0.7)'; ctx.lineWidth=1.8;
  ctx.beginPath(); ctx.ellipse(cx,mouthY,mW/2,mH/2,0,0,Math.PI*2); ctx.stroke();
  if(mH>8){ctx.fillStyle='#d8d8f0';ctx.beginPath();ctx.ellipse(cx,mouthY-mH*0.1,mW/2-2,mH/3,0,0,Math.PI*2);ctx.fill();}
  const sg=ctx.createRadialGradient(cx,cy+r+15,4,cx,cy+r+15,75);
  sg.addColorStop(0,'rgba(28,28,78,0.5)'); sg.addColorStop(1,'rgba(10,10,50,0)');
  ctx.fillStyle=sg; ctx.beginPath(); ctx.ellipse(cx,cy+r+16,80,45,0,0,Math.PI*2); ctx.fill();
}
let lastT=0;
function loop(ts){
  const dt=Math.min((ts-lastT)/1000,0.05); lastT=ts;
  mouth+=(tMouth-mouth)*Math.min(dt*15,1); mouth=Math.max(0,Math.min(1,mouth));
  blinkT+=dt;
  if(blinkT>3+Math.random()*2.5){blinkA=1;setTimeout(()=>{blinkA=0},130);blinkT=0;}
  aLvl+=(tLvl-aLvl)*0.11; tLvl*=0.92;
  draw();
  document.getElementById('level-bar').style.width=(aLvl*100)+'%';
  requestAnimationFrame(loop);
}

let ac=null, an=null, da=null;
function setupAnalyser(stream){
  ac=new (window.AudioContext||window.webkitAudioContext)();
  an=ac.createAnalyser(); an.fftSize=512; an.smoothingTimeConstant=0.7;
  da=new Uint8Array(an.frequencyBinCount);
  const src=ac.createMediaStreamSource(stream); src.connect(an);
}
function updateMouth(){
  if(!an)return; an.getByteFrequencyData(da);
  let s=0; for(let i=0;i<da.length/4;i++)s+=da[i];
  const avg=s/(da.length/4);
  tMouth=Math.min(1,avg/85); tLvl=Math.min(1,avg/105);
}

let stream=null, interval=null;
async function startMic(){
  try{
    stream=await navigator.mediaDevices.getUserMedia({audio:true,video:false});
    setupAnalyser(stream);
    interval=setInterval(updateMouth,33);
    document.getElementById('micBtn').textContent='🎤 Listening...';
    document.getElementById('micBtn').classList.add('active');
    document.getElementById('sub').textContent="Speak - I'm listening";
    document.getElementById('transcript').textContent='Listening...';
    setInterval(()=>{
      document.getElementById('latency').textContent='● LIVE';
      document.getElementById('latency').style.color='#28c028';
    },2000);
  }catch(e){
    document.getElementById('transcript').innerHTML='<span style="color:#ff5050">Mic: '+e.message+'</span>';
  }
}
function stopMic(){
  if(stream){stream.getTracks().forEach(t=>t.stop());stream=null;}
  if(interval){clearInterval(interval);interval=null;}
  tMouth=0; tLvl=0;
  document.getElementById('micBtn').textContent='🎤 Start';
  document.getElementById('micBtn').classList.remove('active');
  document.getElementById('sub').textContent='Real-time AI voice assistant';
  document.getElementById('level-bar').style.width='0%';
}
document.getElementById('micBtn').addEventListener('click',()=>{
  if(talking){stopMic();talking=false;}
  else{startMic();talking=true;}
});
requestAnimationFrame(loop);
document.getElementById('transcript').textContent='Ready - click Start';
</script>
</body>
</html>
"""

# ─── OLLAMA HELPERS ──────────────────────────────────────────────────────────
def start_ollama():
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=3)
        if r.status_code == 200: print("[OLLAMA] Already running"); return True
    except: pass
    import subprocess, time
    print("[OLLAMA] Starting..."); log = open(BASE_DIR/"ollama.log","w")
    proc = subprocess.Popen([str(OLLAMA_EXE),"serve"], cwd=str(BASE_DIR),
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    for i in range(12):
        time.sleep(2)
        try:
            r = requests.get("http://localhost:11434/api/tags", timeout=3)
            if r.status_code == 200: print("[OLLAMA] Ready"); return True
        except: pass
        print(f"[OLLAMA] Waiting... {i+1}/12")
    return True

def ensure_model():
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models",[])]
        base = OLLAMA_MODEL.split(":")[0]
        if any(base in n for n in models):
            print(f"[OLLAMA] Model '{OLLAMA_MODEL}' present"); return True
        print(f"[OLLAMA] Pulling '{OLLAMA_MODEL}'...")
        r2 = requests.post("http://localhost:11434/api/pull",
            json={"name": OLLAMA_MODEL}, stream=True, timeout=600)
        for line in r2.iter_lines():
            if line:
                try:
                    d = __import__('json').loads(line)
                    s = d.get("status","")
                    if s: print(f"\r[OLLAMA] {s[:60]}", end="", flush=True)
                except: pass
        print("\n[OLLAMA] Model ready"); return True
    except Exception as e:
        print(f"[OLLAMA] Model error: {e}"); return False

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    global PORT, OLLAMA_MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--model", type=str, default=OLLAMA_MODEL)
    args = ap.parse_args()
    PORT = args.port; OLLAMA_MODEL = args.model

    print(f"\n{'='*56}")
    print(f"  Jupitercore Avatar Server  (FastRTC WebRTC)")
    print(f"  GPU:     {GPU_NAME} ({VRAM_GB:.0f}GB)")
    print(f"  Port:    {PORT}")
    print(f"  Model:   {OLLAMA_MODEL}")
    print(f"  TTS:     VoxCPM2 (subprocess)")
    print(f"  Avatar:  Canvas (browser-side)")
    print(f"{'='*56}\n")

    start_ollama()
    ensure_model()

    print("[INIT] Building FastRTC Stream...")
    stream = Stream(
        handler=ReplyOnPause(avatar_handler),
        modality="audio",
        mode="send-receive",
    )
    
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    app = FastAPI()
    
    @app.get("/")
    async def root(): return HTMLResponse(HTML)
    @app.get("/health")
    async def health():
        return {"status":"ok","gpu":GPU_NAME,"vram_gb":round(VRAM_GB,1),
                "device":DEVICE,"ollama":OLLAMA_MODEL}
    
    stream.mount(app, "/stream")

    import uvicorn
    print(f"\n  Server ready: http://100.71.215.1:{PORT}\n")
    print("  (or http://localhost:{})".format(PORT))
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

if __name__ == "__main__":
    main()
