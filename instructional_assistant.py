"""Desktop Instructional Assistant.

A small Tkinter window that streams microphone audio to the OpenAI Realtime API
and plays the spoken reply. A circle in the window shows the state:
gray when idle, pulsing blue while listening, pulsing violet while speaking.

Configuration comes from environment variables or a .env file next to this
script. See .env.example.
"""

import asyncio
import base64
import json
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import wave
from pathlib import Path
from tkinter import messagebox, simpledialog

import openai
import pygame
import sounddevice as sd
import websockets
from dotenv import load_dotenv

# ============================================================
# Configuration
# ============================================================

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
load_dotenv(Path.cwd() / ".env")
if getattr(sys, "frozen", False):  # packaged .exe: read .env next to the executable
    load_dotenv(Path(sys.executable).resolve().parent / ".env")
else:
    load_dotenv(Path(__file__).resolve().parent / ".env")

REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime-2.1")
ASSISTANT_VOICE = os.getenv("ASSISTANT_VOICE", "verse")
ASSISTANT_PROFILE = os.getenv("ASSISTANT_PROFILE", "automation").strip().lower()
WINDOW_TITLE = os.getenv("WINDOW_TITLE", "Instructional Assistant")

PROFILE_DIR = BASE_DIR / "profiles"


def load_profile(name: str) -> str:
    """Read the system instructions for the chosen profile."""
    path = PROFILE_DIR / f"{name}.md"
    if not path.exists():
        available = ", ".join(p.stem for p in PROFILE_DIR.glob("*.md")) or "none found"
        raise SystemExit(
            f"Profile '{name}' not found in {PROFILE_DIR}. Available: {available}"
        )
    return path.read_text(encoding="utf-8")


SYSTEM_INSTRUCTIONS = load_profile(ASSISTANT_PROFILE)

# ============================================================
# Global state
# ============================================================

running = False
session_thread = None
root = None
status_label = None
canvas = None
pulse_circle = None
listen_button = None
animation_id = None

# ============================================================
# API key
# ============================================================


def key_is_valid(key: str) -> bool:
    try:
        openai.api_key = key
        openai.models.list()
        return True
    except Exception:
        return False


def get_api_key() -> str:
    """Use OPENAI_API_KEY if set; otherwise ask once. The key is never written to disk."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key and key_is_valid(key):
        return key

    prompt_root = tk.Tk()
    prompt_root.withdraw()
    try:
        while True:
            key = simpledialog.askstring(
                "OpenAI API Key",
                "Enter your OpenAI API key.\nPress Cancel to quit.",
                show="*",
                parent=prompt_root,
            )
            if key is None:  # Cancel or window closed
                sys.exit(0)
            key = key.strip()
            if key and key_is_valid(key):
                return key
            messagebox.showerror(
                "Invalid API Key",
                "That key did not work. Try again or press Cancel to quit.",
                parent=prompt_root,
            )
    finally:
        prompt_root.destroy()


API_KEY = get_api_key()
WS_URL = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "OpenAI-Beta": "realtime=v1"}

# ============================================================
# GUI pulse animation
# ============================================================


def animate_pulse(color, scale_up=True):
    global animation_id
    if canvas is None or pulse_circle is None or root is None:
        return
    x0, _, x1, _ = canvas.coords(pulse_circle)
    cx, cy = 50, 50
    r = (x1 - x0) / 2
    step = 2 if scale_up else -2
    new_r = max(20, min(30, r + step))
    canvas.coords(pulse_circle, cx - new_r, cy - new_r, cx + new_r, cy + new_r)
    canvas.itemconfig(pulse_circle, fill=color)
    if animation_id:
        root.after_cancel(animation_id)
    animation_id = root.after(100, lambda: animate_pulse(color, not scale_up))


def start_listening_animation():
    animate_pulse("lightblue")


def start_speaking_animation():
    animate_pulse("plum")


def reset_pulse():
    global animation_id
    if animation_id:
        root.after_cancel(animation_id)
        animation_id = None
    canvas.coords(pulse_circle, 25, 25, 75, 75)
    canvas.itemconfig(pulse_circle, fill="lightgray")


def reset_label_and_pulse():
    if status_label:
        status_label.config(text="Idle. Press Listen to start.")
    reset_pulse()


# ============================================================
# Audio capture
# ============================================================

AUDIO_RATE = 24000  # 24 kHz mono, 16-bit PCM, as the Realtime API expects
audio_queue = queue.Queue()


def audio_callback(indata, frames, time_info, status):
    if status:
        print(status)
    audio_queue.put(indata.copy())


# ============================================================
# Non-blocking audio playback
# ============================================================


def start_playback_thread(audio_bytes: bytes) -> threading.Thread:
    def _play():
        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(AUDIO_RATE)
                wf.writeframes(audio_bytes)
            pygame.mixer.init(frequency=AUDIO_RATE, size=-16, channels=1, buffer=512)
            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.play()
            clock = pygame.time.Clock()
            while pygame.mixer.music.get_busy():
                clock.tick(50)
            pygame.mixer.music.unload()
        finally:
            try:
                pygame.mixer.quit()
            except Exception:
                pass
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    t = threading.Thread(target=_play, daemon=True)
    t.start()
    return t


# ============================================================
# Realtime session
# ============================================================


async def run_realtime_session():
    global running

    DEBOUNCE_SECONDS = 1.6  # silence required before the assistant answers
    MIN_TURN_SECONDS = 0.7  # ignore very short sounds

    try:
        async with websockets.connect(
            WS_URL,
            additional_headers=HEADERS,
            ping_interval=20,
            ping_timeout=60,
            close_timeout=5,
            max_size=None,
        ) as ws:
            print(f"[Realtime] Connected ({REALTIME_MODEL}, profile: {ASSISTANT_PROFILE})")

            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "voice": ASSISTANT_VOICE,
                    "language": "en",
                    "temperature": 0.3,
                    "instructions": SYSTEM_INSTRUCTIONS,
                },
            }))

            stream = sd.InputStream(
                samplerate=AUDIO_RATE,
                channels=1,
                dtype="int16",
                callback=audio_callback,
            )
            stream.start()
            print("[Mic] Streaming")

            start_listening_animation()
            status_label.config(text="Listening...")

            audio_bytes = b""
            speaking_playback = False
            playback_thread = None
            turn_pending = False

            commit_deadline = None
            last_speech_started = None
            last_speech_stopped = None

            def resume_listening():
                nonlocal speaking_playback, turn_pending
                speaking_playback = False
                turn_pending = False
                reset_pulse()
                start_listening_animation()
                status_label.config(text="Listening...")
                try:
                    stream.start()
                except Exception:
                    pass

            while running:
                # Send microphone frames, except while the assistant is speaking.
                # Pausing the mic during playback keeps the assistant from hearing itself.
                while not audio_queue.empty() and not speaking_playback:
                    frame = audio_queue.get()
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(frame).decode("utf-8"),
                    }))
                    commit_deadline = None

                await asyncio.sleep(0.02)

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
                except asyncio.TimeoutError:
                    raw = None

                if raw:
                    msg = json.loads(raw)
                    typ = msg.get("type")

                    if typ in ("conversation.speech_started", "input_audio_buffer.speech_started"):
                        last_speech_started = time.monotonic()
                        commit_deadline = None

                    elif typ in ("conversation.speech_stopped", "input_audio_buffer.speech_stopped"):
                        last_speech_stopped = time.monotonic()
                        commit_deadline = last_speech_stopped + DEBOUNCE_SECONDS

                    elif typ in ("response.text.delta", "response.output_text.delta"):
                        delta = msg.get("delta", "")
                        if delta:
                            current = status_label.cget("text")
                            if current in ("Listening...", "Processing..."):
                                status_label.config(text=delta)
                            else:
                                status_label.config(text=current + delta)

                    elif typ in ("response.audio.delta", "response.output_audio.delta"):
                        if not speaking_playback:
                            print("[Playback] Speaking")
                            status_label.config(text="Speaking...")
                            start_speaking_animation()
                            try:
                                stream.stop()
                            except Exception:
                                pass
                            speaking_playback = True
                        audio_b64 = msg.get("delta") or msg.get("audio")
                        if audio_b64:
                            audio_bytes += base64.b64decode(audio_b64)

                    elif typ in ("response.done", "response.completed"):
                        if audio_bytes:
                            playback_thread = start_playback_thread(audio_bytes)
                            audio_bytes = b""
                        else:
                            resume_listening()

                    elif typ in ("response.refused", "response.error", "error"):
                        print(f"[Response Error] {msg}")
                        status_label.config(text="I could not answer that.")
                        resume_listening()

                # Playback finished: go back to listening.
                if speaking_playback and playback_thread is not None and not playback_thread.is_alive():
                    playback_thread = None
                    resume_listening()

                # Answer only after the speaker has been quiet for DEBOUNCE_SECONDS.
                if commit_deadline is not None and not speaking_playback and not turn_pending:
                    now = time.monotonic()
                    if last_speech_started is not None and last_speech_stopped is not None:
                        span = last_speech_stopped - last_speech_started
                    else:
                        span = 0.0
                    if now >= commit_deadline and span >= MIN_TURN_SECONDS:
                        status_label.config(text="Processing...")
                        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        await ws.send(json.dumps({
                            "type": "response.create",
                            "response": {"instructions": SYSTEM_INSTRUCTIONS},
                        }))
                        turn_pending = True
                        commit_deadline = None

            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    except Exception as e:
        print(f"[Realtime Error] {e}")
        if status_label:
            status_label.config(text=f"Error: {e}")
        reset_pulse()


# ============================================================
# Session control
# ============================================================


def start_session():
    global session_thread, running
    if session_thread is None or not session_thread.is_alive():
        running = True
        session_thread = threading.Thread(
            target=lambda: asyncio.run(run_realtime_session()), daemon=True
        )
        session_thread.start()
        listen_button.config(text="Stop", command=stop_session)


def stop_session():
    global running
    running = False
    listen_button.config(text="Listen", command=start_session)
    reset_label_and_pulse()


def on_close():
    global running
    running = False
    if root:
        root.destroy()
    sys.exit(0)


# ============================================================
# Window
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    root.title(WINDOW_TITLE)
    root.geometry("560x260")
    root.protocol("WM_DELETE_WINDOW", on_close)

    status_label = tk.Label(
        root, text="Press Listen to begin", font=("Arial", 14), wraplength=520, justify="center"
    )
    status_label.pack(pady=20)

    canvas = tk.Canvas(root, width=100, height=100, bg="white", highlightthickness=0)
    canvas.pack(pady=5)
    pulse_circle = canvas.create_oval(25, 25, 75, 75, fill="lightgray")

    listen_button = tk.Button(root, text="Listen", font=("Arial", 12), command=start_session)
    listen_button.pack(pady=10)

    root.mainloop()
