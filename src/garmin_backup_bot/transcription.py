from __future__ import annotations

import asyncio
import io
import logging

logger = logging.getLogger(__name__)


class Transcriber:
    """Universal voice-to-text transcription using OpenAI Whisper API."""

    def __init__(self, api_key: str) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)

    async def transcribe(
        self,
        buf: io.BytesIO,
        mime_type: str = "audio/ogg",
    ) -> str:
        """Transcribe audio buffer to text.

        Args:
            buf: Audio data (e.g. OGG from Telegram voice messages).
            mime_type: MIME type of the audio.

        Returns:
            Transcribed text string.
        """
        ext = {
            "audio/ogg": "ogg",
            "audio/mpeg": "mp3",
            "audio/mp4": "m4a",
            "audio/wav": "wav",
        }.get(mime_type, "ogg")
        buf.name = f"voice.{ext}"
        buf.seek(0)

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.audio.transcriptions.create,
                    model="whisper-1",
                    file=buf,
                    language="ru",
                ),
                timeout=60.0,
            )
            text = response.text.strip()
            if not text:
                raise RuntimeError("Не удалось распознать речь. Попробуй ещё раз.")
            logger.info("Transcription OK: %d chars", len(text))
            return text
        except asyncio.TimeoutError:
            logger.error("Transcription timed out after 60s")
            raise RuntimeError("Транскрибация заняла слишком долго. Попробуй короче.") from None
        except RuntimeError:
            raise
        except Exception as exc:
            logger.exception("Transcription failed")
            raise RuntimeError("Ошибка транскрибации. Попробуй ещё раз.") from exc
