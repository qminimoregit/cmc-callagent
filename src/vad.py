# src/vad.py
from __future__ import annotations
"""
Voice Activity Detection using Google WebRTC VAD.

Designed for Twilio 8 kHz MULAW audio streams.
Each call creates one VADProcessor instance.  Feed raw MULAW chunks from
Twilio's <Stream> media events into process_mulaw_chunk(); it returns
(True, pcm_bytes) as soon as a complete utterance has been detected.

Tuning knobs
────────────
aggressiveness        0 (lenient) – 3 (aggressive noise rejection)
silence_trigger_ms    How many ms of trailing silence end an utterance.
                      300 ms (~15 × 20 ms frames) is a good default for
                      conversational speech; increase to 400–500 ms if
                      callers are being cut off mid-sentence.
min_speech_ms         Ignore bursts shorter than this (filters out clicks,
                      breathing, DTMF tones).
"""

import audioop
import collections
import logging
from typing import NamedTuple

import webrtcvad

logger = logging.getLogger(__name__)

# ── Audio constants (must match Twilio phone stream) ──────────────────────────
SAMPLE_RATE        = 8000          # Hz — Twilio MULAW phone audio
FRAME_DURATION_MS  = 20            # ms  — webrtcvad supports 10 / 20 / 30
BYTES_PER_SAMPLE   = 2             # LINEAR16 = 2 bytes per sample
FRAME_SAMPLES      = SAMPLE_RATE * FRAME_DURATION_MS // 1000   # 160 samples
FRAME_BYTES        = FRAME_SAMPLES * BYTES_PER_SAMPLE           # 320 bytes (PCM)


class UtteranceResult(NamedTuple):
    complete: bool       # True when a full utterance has been captured
    pcm: bytes           # LINEAR16 PCM of the utterance (non-empty when complete=True)


class VADProcessor:
    """
    Accumulates raw MULAW chunks from Twilio, converts them to LINEAR16 PCM
    frames, and runs WebRTC VAD frame-by-frame.

    State machine
    ─────────────
    IDLE     → not yet in an utterance
    SPEAKING → voiced frames detected, accumulating audio
    (after N consecutive silent frames) → emit utterance, reset to IDLE

    A ring-buffer of pre-speech frames is prepended so the very start of
    the word is not clipped.
    """

    def __init__(
        self,
        aggressiveness: int = 2,
        silence_trigger_ms: int = 300,
        min_speech_ms: int = 100,
        pre_speech_pad_ms: int = 100,
    ):
        self._vad = webrtcvad.Vad(aggressiveness)

        self._silence_frames   = silence_trigger_ms  // FRAME_DURATION_MS   # 15
        self._min_speech_frames = min_speech_ms       // FRAME_DURATION_MS   # 5
        pre_pad_frames         = pre_speech_pad_ms    // FRAME_DURATION_MS   # 5

        # Ring buffer holds the last N frames before speech starts
        self._pre_buffer: collections.deque[bytes] = collections.deque(maxlen=pre_pad_frames)

        self._triggered       = False
        self._voiced_frames   : list[bytes] = []
        self._speech_count    = 0    # frames of speech seen since triggered
        self._silent_count    = 0    # consecutive silent frames after speech
        self._pcm_tail        = b""  # leftover PCM bytes < one full frame

        logger.debug(
            "VADProcessor init: aggressiveness=%d  silence_trigger=%dms  "
            "min_speech=%dms  pre_pad=%dms",
            aggressiveness, silence_trigger_ms, min_speech_ms, pre_speech_pad_ms,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def process_mulaw_chunk(self, mulaw_bytes: bytes) -> UtteranceResult:
        """
        Feed one Twilio media chunk (any byte length, 8 kHz MULAW).

        Returns UtteranceResult(complete=True, pcm=<audio>) the first time a
        complete utterance is detected after this chunk; otherwise returns
        UtteranceResult(complete=False, pcm=b'').
        """
        # MULAW → LINEAR16 PCM
        pcm = audioop.ulaw2lin(mulaw_bytes, BYTES_PER_SAMPLE)
        self._pcm_tail += pcm

        # Process as many full frames as available
        while len(self._pcm_tail) >= FRAME_BYTES:
            frame, self._pcm_tail = (
                self._pcm_tail[:FRAME_BYTES],
                self._pcm_tail[FRAME_BYTES:],
            )
            result = self._process_frame(frame)
            if result.complete:
                return result

        return UtteranceResult(complete=False, pcm=b"")

    def reset(self):
        """Manually reset state (e.g. when a new call starts)."""
        self._triggered       = False
        self._voiced_frames   = []
        self._speech_count    = 0
        self._silent_count    = 0
        self._pcm_tail        = b""
        self._pre_buffer.clear()
        logger.debug("VADProcessor reset")

    @property
    def is_speaking(self) -> bool:
        """True while an utterance is in progress (speech detected, not yet ended)."""
        return self._triggered

    # ── Internal frame processing ─────────────────────────────────────────────

    def _process_frame(self, frame: bytes) -> UtteranceResult:
        try:
            is_speech = self._vad.is_speech(frame, SAMPLE_RATE)
        except Exception as exc:
            logger.warning("webrtcvad error: %s", exc)
            is_speech = False

        if not self._triggered:
            # ── IDLE: look for speech onset ───────────────────────────────────
            self._pre_buffer.append(frame)
            if is_speech:
                self._triggered = True
                # Prepend pre-roll frames so the first phoneme isn't cut
                self._voiced_frames = list(self._pre_buffer)
                self._pre_buffer.clear()
                self._speech_count = 1
                self._silent_count = 0
                logger.debug("VAD: speech onset detected")
        else:
            # ── SPEAKING: accumulate and watch for silence ────────────────────
            self._voiced_frames.append(frame)

            if is_speech:
                self._speech_count += 1
                self._silent_count  = 0
            else:
                self._silent_count += 1
                if self._silent_count >= self._silence_frames:
                    # Enough trailing silence — utterance complete
                    if self._speech_count >= self._min_speech_frames:
                        audio = b"".join(self._voiced_frames)
                        logger.info(
                            "VAD: utterance complete — %d ms speech, "
                            "%d ms trailing silence, %d PCM bytes",
                            self._speech_count  * FRAME_DURATION_MS,
                            self._silent_count  * FRAME_DURATION_MS,
                            len(audio),
                        )
                        self._reset_state()
                        return UtteranceResult(complete=True, pcm=audio)
                    else:
                        # Too short — probably noise; discard and reset
                        logger.debug(
                            "VAD: discarding short burst (%d ms)",
                            self._speech_count * FRAME_DURATION_MS,
                        )
                        self._reset_state()

        return UtteranceResult(complete=False, pcm=b"")

    def _reset_state(self):
        self._triggered      = False
        self._voiced_frames  = []
        self._speech_count   = 0
        self._silent_count   = 0
        # Do NOT clear _pcm_tail — may contain the start of the next utterance
