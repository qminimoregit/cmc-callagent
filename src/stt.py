# src/stt.py
from __future__ import annotations
"""
Google Cloud Speech-to-Text integration.
Configured for trilingual recognition: Sinhala (si-LK), Tamil (ta-LK), English (en-US).
Optimised for Twilio phone-call audio (8 kHz LINEAR16 PCM).
"""

import logging
import httpx
import asyncio
import queue
import threading
from typing import Optional, Generator, Iterator
from google.cloud import speech

logger = logging.getLogger(__name__)

# ⚡ Pre-warm clients at module level — avoids ~300-500ms cold start per request
_speech_client: speech.SpeechClient | None = None

def _get_speech_client() -> speech.SpeechClient:
    """Lazy-initialise a module-level SpeechClient singleton."""
    global _speech_client
    if _speech_client is None:
        _speech_client = speech.SpeechClient()
        logger.info("STT client initialised (singleton)")
    return _speech_client

# ⚡ Persistent HTTP client with connection pooling + HTTP/2 for Twilio downloads
_http_client: httpx.Client | None = None

def _get_http_client() -> httpx.Client:
    """Lazy-initialise a persistent httpx.Client with connection reuse."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=15,
            follow_redirects=True,
            http2=True,
        )
        logger.info("HTTP client initialised (persistent, HTTP/2)")
    return _http_client

# Mapping from BCP-47 language tag → our internal code
# Keys are lowercased to match detected_bcp47.lower() lookup below
_LANG_TAG_MAP: dict[str, str] = {
    "si-lk": "si",
    "ta-lk": "ta",
    "en-us": "en",
    "en-gb": "en",
}


from typing import Optional

def get_stt_config(
    sample_rate_hertz: Optional[int] = 16000,
    encoding: speech.RecognitionConfig.AudioEncoding = speech.RecognitionConfig.AudioEncoding.LINEAR16,
    primary_lang: str = "si-LK",
) -> speech.RecognitionConfig:
    """
    Return a RecognitionConfig with trilingual auto-detection.

    Parameters
    ----------
    sample_rate_hertz : int, optional
        8000 for Twilio phone-call audio, 16000 for microphone input (default).
        Can be None for WEBM_OPUS so it infers from the file header.
    encoding: speech.RecognitionConfig.AudioEncoding
        Audio encoding format, default LINEAR16.
    primary_lang: str
        BCP-47 primary language code. Defaults to 'si-LK'.
        When 'en-US' is primary, we use latest_short for lower latency.
    """
    # ⚡ Fix 6: Use latest_short for English (faster for short utterances);
    # keep 'default' for Sinhala/Tamil since latest_short doesn't support them.
    model = "latest_short" if primary_lang == "en-US" else "default"

    kwargs = {
        "encoding": encoding,
        "language_code": primary_lang,
        "alternative_language_codes": [
            c for c in ["si-LK", "ta-LK", "en-US"] if c != primary_lang
        ],
        "enable_automatic_punctuation": True,
        "model": model,
        "use_enhanced": True,
    }
    if sample_rate_hertz is not None:
        kwargs["sample_rate_hertz"] = sample_rate_hertz

    return speech.RecognitionConfig(**kwargs)


def transcribe(audio_bytes: bytes, sample_rate_hertz: Optional[int] = 16000, encoding: speech.RecognitionConfig.AudioEncoding = speech.RecognitionConfig.AudioEncoding.LINEAR16) -> tuple[str, str, float]:
    """
    Transcribe raw LINEAR16 or WEBM_OPUS audio bytes to text.

    Parameters
    ----------
    audio_bytes : bytes
        Raw audio bytes.
    sample_rate_hertz : int, optional
        8000 for Twilio, 16000/48000 for microphone. None for WEBM_OPUS.
    encoding: speech.RecognitionConfig.AudioEncoding
        Audio format.

    Returns
    -------
    transcript : str
        The recognised text.
    lang_code : str
        Internal language code: 'si', 'ta', or 'en'.
        Defaults to 'si' if detection fails.
    """
    client = _get_speech_client()
    audio = speech.RecognitionAudio(content=audio_bytes)
    config = get_stt_config(sample_rate_hertz=sample_rate_hertz, encoding=encoding)

    try:
        response = client.recognize(config=config, audio=audio)
    except Exception as exc:
        logger.error("STT recognition failed: %s", exc)
        return "", "si", 0.0

    if not response.results:
        logger.warning("STT returned no results.")
        print("STT returned no results.")
        return "", "si", 0.0

    best_result = response.results[0]
    best_alt = best_result.alternatives[0]
    transcript = best_alt.transcript.strip()

    # Extract detected language from the result metadata
    detected_bcp47: str = getattr(best_result, "language_code", "si-LK") or "si-LK"
    lang_code = _LANG_TAG_MAP.get(detected_bcp47.lower(), "si")

    logger.info("STT → transcript=%r  lang=%s  confidence=%.2f",
                transcript, lang_code, best_alt.confidence)
    return transcript, lang_code, best_alt.confidence


def transcribe_pcm(pcm_bytes: bytes, sample_rate: int = 8000) -> tuple[str, str, float]:
    """
    Transcribe raw LINEAR16 PCM bytes produced by VADProcessor.

    This is the fast path used by the WebSocket media-stream handler:
    VAD captures the utterance as LINEAR16 PCM at 8 kHz, so we bypass
    MULAW decoding and hand it straight to Google STT.

    Parameters
    ----------
    pcm_bytes : bytes
        Raw LINEAR16 PCM audio (16-bit, mono, little-endian).
    sample_rate : int
        Sample rate in Hz. Twilio phone streams use 8000 Hz.

    Returns
    -------
    transcript : str
    lang_code  : str   ('si', 'ta', or 'en')
    confidence : float
    """
    return transcribe(
        pcm_bytes,
        sample_rate_hertz=sample_rate,
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
    )


def transcribe_from_url(audio_url: str) -> tuple[str, str, float]:
    """
    Transcribe audio from a Twilio recording URL.
    Auto-detects WAV vs MP3 from the URL extension and uses the correct
    Google STT encoding.
      - .wav  → LINEAR16 at 8000 Hz  (Twilio phone-quality PCM)
      - .mp3  → MP3 encoding, sample rate inferred from header
    """
    import os

    logger.info("Downloading audio from %s", audio_url)

    # Twilio recording URLs require HTTP Basic Auth (Account SID + Auth Token)
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token  = os.getenv("TWILIO_AUTH_TOKEN", "")
    auth = (account_sid, auth_token) if account_sid and auth_token else None

    # ⚡ Reuse persistent HTTP client — saves TCP/TLS handshake time
    resp = _get_http_client().get(audio_url, auth=auth)
    resp.raise_for_status()

    # Choose encoding based on URL extension
    if audio_url.endswith(".wav"):
        return transcribe(
            resp.content,
            sample_rate_hertz=8000,
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        )
    else:
        # MP3 — let Google infer sample rate from the file header
        return transcribe(
            resp.content,
            sample_rate_hertz=None,
            encoding=speech.RecognitionConfig.AudioEncoding.MP3,
        )


class StreamingTranscriber:
    """
    Handles real-time streaming STT from Twilio Media Streams.
    Pipes chunks into Google's streaming_recognize and yields results.
    """
    def __init__(self, locked_lang: str = "si"):
        self.client = _get_speech_client()
        self.locked_lang = locked_lang
        
        # Twilio sends 8kHz MULAW
        self.config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code=self._get_bcp47(locked_lang),
            alternative_language_codes=["si-LK", "ta-LK", "en-US"],
            enable_automatic_punctuation=True,
            model="latest_short", # Optimized for short utterances
        )
        self.streaming_config = speech.StreamingRecognitionConfig(
            config=self.config,
            interim_results=True,
        )
        self._audio_queue = queue.Queue()
        self.closed = False

    def _get_bcp47(self, lang: str) -> str:
        mapping = {"si": "si-LK", "ta": "ta-LK", "en": "en-US"}
        return mapping.get(lang, "si-LK")

    def fill_buffer(self, chunk: bytes):
        """Add audio chunk to the queue."""
        self._audio_queue.put(chunk)

    def generator(self) -> Generator[bytes, None, None]:
        while not self.closed:
            chunk = self._audio_queue.get()
            if chunk is None:
                return
            data = [chunk]
            while True:
                try:
                    chunk = self._audio_queue.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break
            yield b"".join(data)

    def stream(self) -> Iterator[speech.StreamingRecognizeResponse]:
        """Generator that yields recognition responses."""
        requests = (
            speech.StreamingRecognizeRequest(audio_content=content)
            for content in self.generator()
        )
        return self.client.streaming_recognize(self.streaming_config, requests)

    def close(self):
        self.closed = True
        self._audio_queue.put(None)


class RealtimeTranscriber:
    """
    ⚡ Low-latency STT that runs DURING speech (not after).

    Usage pattern (in the WebSocket media handler):
      1. When VAD detects speech ONSET → call start()
      2. For each PCM frame while speaking → call feed(frame)
      3. When VAD fires (utterance complete) → call finish()
         Returns (transcript, lang, confidence) — already mostly ready.

    This overlaps STT with speaking time, saving 300–600 ms compared to
    the batch recognize() call that happens after VAD fires.
    """

    _LANG_MAP = {"si": "si-LK", "ta": "ta-LK", "en": "en-US"}

    def __init__(self, lang: str = "si", sample_rate: int = 8000):
        self._client      = _get_speech_client()
        self._lang        = lang
        self._sample_rate = sample_rate
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._result: tuple[str, str, float] = ("", lang, 0.0)

        bcp47 = self._LANG_MAP.get(lang, "si-LK")
        alt   = [v for v in self._LANG_MAP.values() if v != bcp47]
        # ⚡ Fix 6: Use latest_short for English (lower latency)
        model = "latest_short" if bcp47 == "en-US" else "default"
        self._streaming_config = speech.StreamingRecognitionConfig(
            config=speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=sample_rate,
                language_code=bcp47,
                alternative_language_codes=alt,
                enable_automatic_punctuation=True,
                model=model,
                use_enhanced=True,
            ),
            # ⚡ Fix 3: Enable interim results — Google STT returns partials faster,
            # meaning the stream closes sooner after VAD fires. We track the last
            # interim as a fallback if no final result arrives.
            interim_results=True,
        )

    def start(self) -> None:
        """Call when VAD detects speech onset. Opens the STT stream immediately."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def feed(self, pcm_frame: bytes) -> None:
        """Call for each LINEAR16 PCM frame while the caller is speaking."""
        self._queue.put(pcm_frame)

    def finish(self) -> tuple[str, str, float]:
        """
        Call when VAD fires (utterance complete).
        Signals end-of-stream and waits (briefly) for the final transcript.
        By this point, STT has already processed most of the audio.
        """
        self._queue.put(None)  # sentinel → ends generator
        if self._thread:
            self._thread.join(timeout=3.0)  # should already be done or very close
        return self._result

    def _run(self) -> None:
        """Background thread: feeds audio into Google STT streaming API."""
        def _gen() -> Generator[speech.StreamingRecognizeRequest, None, None]:
            while True:
                chunk = self._queue.get()
                if chunk is None:
                    return
                yield speech.StreamingRecognizeRequest(audio_content=chunk)

        # ⚡ Fix 3: Track last interim so we have a fallback if no final arrives
        _last_interim: str = ""
        _last_interim_lang: str = self._lang

        try:
            responses = self._client.streaming_recognize(self._streaming_config, _gen())
            for resp in responses:
                if not resp.results:
                    continue
                result = resp.results[0]
                if not result.alternatives:
                    continue
                alt      = result.alternatives[0]
                detected = getattr(result, "language_code", "") or ""
                lang_code = _LANG_TAG_MAP.get(detected.lower(), self._lang)
                if result.is_final:
                    self._result = (alt.transcript.strip(), lang_code, alt.confidence)
                    logger.info(
                        "RealtimeSTT final → %r  lang=%s  conf=%.2f",
                        self._result[0], self._result[1], self._result[2],
                    )
                    return  # no need to wait for more results
                else:
                    # Keep the most recent interim as a safety net
                    _last_interim = alt.transcript.strip()
                    _last_interim_lang = lang_code
                    logger.debug("RealtimeSTT interim → %r", _last_interim)
        except Exception as exc:
            logger.error("RealtimeTranscriber error: %s", exc, exc_info=True)

        # ⚡ Fallback: if stream ended with no final result, use last interim
        if not self._result[0] and _last_interim:
            logger.info(
                "RealtimeSTT: no final result — using last interim %r  lang=%s",
                _last_interim, _last_interim_lang,
            )
            self._result = (_last_interim, _last_interim_lang, 0.5)


