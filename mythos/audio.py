"""
MYTHOS — audio alerts (Requirement §10).

Reuses run7's proven dual-path playback:
    MP3  → pygame.mixer.music (the only pygame API that decodes MP3),
           serialized by a lock, fired from a daemon thread.
    tone → pygame.mixer.Sound with a synthesized buffer when a file is missing.

Sound files live in mythos/assets/: alert_entry.mp3, alert_win.mp3,
alert_loss.mp3, alert_commentary.mp3 (optional). The launcher copies them
from run7 on first start if absent.
"""

import logging
import math
import os
import struct
import threading

from . import config

log = logging.getLogger("mythos.audio")

_FILES = {
    "entry":      os.path.join(config.ASSETS_DIR, "alert_entry.mp3"),
    "win":        os.path.join(config.ASSETS_DIR, "alert_win.mp3"),
    "loss":       os.path.join(config.ASSETS_DIR, "alert_loss.mp3"),
    "commentary": os.path.join(config.ASSETS_DIR, "alert_commentary.mp3"),
    "armed":      os.path.join(config.ASSETS_DIR, "alert_armed.mp3"),
}
_TONES = {  # (freq, ms, volume) fallbacks
    "entry":      (1000, 600, 0.90),
    "win":        (880, 500, 0.85),
    "loss":       (330, 700, 0.95),
    "commentary": (1320, 320, 0.60),   # short "ting" — a notification, not a song
    "armed":      (660, 2600, 0.75),   # long two-note chime: ZONE ARMED, look up
}

_ready = False
_sounds: dict = {}        # kind -> Sound object (tones) or path str (mp3)
_is_mp3: dict = {}
_music_lock = threading.Lock()


def _tone_buffer(freq: int, ms: int, volume: float = 0.85) -> bytes:
    sr = 22050
    n = int(sr * ms / 1000)
    buf = bytearray(n * 2)
    for i in range(n):
        fade = 1.0 - (i / n) ** 0.3
        val = int(32767 * volume * fade * math.sin(2 * math.pi * freq * i / sr))
        struct.pack_into("<h", buf, i * 2, max(-32767, min(32767, val)))
    return bytes(buf)


def _chime_buffer(volume: float = 0.7) -> bytes:
    """Commentary chime: a deliberate ~2.6 s two-note bell (660→880 Hz with a
    soft third harmonic), long enough to register across the room — the user
    explicitly wants significant commentary to be HEARD, not blipped."""
    sr = 22050
    notes = [(660.0, 1.2), (880.0, 1.4)]          # (freq, seconds)
    total = int(sr * sum(d for _, d in notes))
    buf = bytearray(total * 2)
    pos = 0
    for freq, dur in notes:
        n = int(sr * dur)
        for i in range(n):
            t = i / sr
            env = min(1.0, t / 0.04) * (1.0 - (i / n)) ** 1.4   # attack + decay
            s = (math.sin(2 * math.pi * freq * t)
                 + 0.35 * math.sin(2 * math.pi * freq * 2.0 * t))
            val = int(32767 * volume * env * s / 1.35)
            struct.pack_into("<h", buf, (pos + i) * 2,
                             max(-32767, min(32767, val)))
        pos += n
    return bytes(buf)


def init() -> bool:
    """Initialise the mixer; safe to call once at startup."""
    global _ready
    try:
        import pygame
        pygame.mixer.pre_init(22050, -16, 1, 2048)
        pygame.mixer.init()
        if not pygame.mixer.get_init():
            log.warning("pygame mixer failed to initialise — audio disabled")
            return False
        for kind, path in _FILES.items():
            if os.path.exists(path):
                _sounds[kind] = path
                _is_mp3[kind] = True
            elif kind == "armed":
                # the long two-note chime now means "GET READY — zone armed"
                snd = pygame.mixer.Sound(buffer=_chime_buffer(0.75))
                snd.set_volume(0.75)
                _sounds[kind] = snd
                _is_mp3[kind] = False
            else:
                freq, ms, vol = _TONES[kind]
                snd = pygame.mixer.Sound(buffer=_tone_buffer(freq, ms, vol))
                snd.set_volume(vol)
                _sounds[kind] = snd
                _is_mp3[kind] = False
        _ready = True
        srcs = {k: (os.path.basename(v) if isinstance(v, str) else "tone")
                for k, v in _sounds.items()}
        log.info("Audio ready: %s", srcs)
        return True
    except Exception as e:
        log.warning("Audio init failed: %s", e)
        return False


def play(kind: str):
    """Non-blocking. kind ∈ {'entry','win','loss','commentary'}."""
    if not _ready or kind not in _sounds:
        return
    try:
        import pygame
        if not pygame.mixer.get_init():        # self-heal a dead mixer
            try:
                pygame.mixer.init(22050, -16, 1, 2048)
            except Exception:
                return
        snd = _sounds[kind]
        if _is_mp3.get(kind):
            def _mp3(path: str):
                with _music_lock:
                    try:
                        pygame.mixer.music.load(path)
                        pygame.mixer.music.set_volume(0.95)
                        pygame.mixer.music.play()
                    except Exception as ex:
                        log.debug("mp3 play failed: %s", ex)
            threading.Thread(target=_mp3, args=(snd,), daemon=True).start()
        else:
            snd.play()
    except Exception as e:
        log.debug("play(%s) failed: %s", kind, e)


def copy_run7_assets():
    """One-time: pull the user's existing alert mp3s from run7."""
    import shutil
    src_dir = os.path.join(config.ROOT_DIR, "run7")
    for name in ("alert_entry.mp3", "alert_win.mp3", "alert_loss.mp3"):
        src = os.path.join(src_dir, name)
        dst = os.path.join(config.ASSETS_DIR, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
                log.info("Copied %s from run7", name)
            except Exception as e:
                log.warning("Asset copy failed for %s: %s", name, e)
