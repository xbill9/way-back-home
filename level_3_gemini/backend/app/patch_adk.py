import logging
from google.genai import live, types
from google.adk.models.gemini_llm_connection import GeminiLlmConnection
from google.adk.agents.live_request_queue import LiveRequestQueue, LiveRequest
from google.adk.flows.llm_flows.audio_cache_manager import AudioCacheManager

# Configure a local logger for the patch
logger = logging.getLogger("patch_adk")

_patches_applied = False


def apply_patches():
    """
    Applies monkey-patches to google-genai and google-adk to fix
    Gemini 3.1 Live API compatibility issues (media_chunks deprecation).
    """
    global _patches_applied
    if _patches_applied:
        logger.debug("Patches already applied. Skipping.")
        return
    _patches_applied = True

    logger.info("Applying Gemini 3.1 Live API compatibility patches...")

    # ========================================
    # 0. Patch AudioCacheManager
    # ========================================
    _original_cache_audio = AudioCacheManager.cache_audio

    def _patched_cache_audio(self, invocation_context, audio_blob, cache_type='input'):
        if audio_blob is None or not hasattr(audio_blob, 'data') or audio_blob.data is None:
            # Not a proper audio/media blob, skip caching to avoid 'NoneType' has no len()
            return
        try:
            return _original_cache_audio(self, invocation_context, audio_blob, cache_type)
        except Exception as e:
            logger.warning(f"[PATCH] Skipping audio cache due to error: {e}")
            return

    AudioCacheManager.cache_audio = _patched_cache_audio

    # ========================================
    # 1. Patch google-genai AsyncSession
    # ========================================
    _original_send_realtime_input = live.AsyncSession.send_realtime_input

    async def _patched_send_realtime_input(self, **kwargs):
        # 1. Handle 'media' parameter (legacy in ADK, deprecated in Gemini 3.1)
        if 'media' in kwargs and kwargs['media'] is not None:
            media = kwargs.pop('media')
            if isinstance(media, types.Blob):
                if media.mime_type and media.mime_type.startswith("audio/"):
                    kwargs['audio'] = media
                elif media.mime_type and (media.mime_type.startswith("image/") or media.mime_type.startswith("video/")):
                    kwargs['video'] = media
                elif media.mime_type and media.mime_type.startswith("text/"):
                    kwargs['text'] = media.data.decode('utf-8')
                else:
                    kwargs['audio'] = media
            elif isinstance(media, str):
                kwargs['text'] = media
            else:
                kwargs['video'] = media

        # 2. Handle 'realtime_input' parameter (if it contains media_chunks)
        if 'realtime_input' in kwargs and kwargs['realtime_input'] is not None:
            rt_input = kwargs['realtime_input']
            if hasattr(rt_input, 'media_chunks') and rt_input.media_chunks:
                logger.info("[PATCH] Unrolling 'media_chunks' from realtime_input.")
                for chunk in rt_input.media_chunks:
                    if hasattr(chunk, 'mime_type') and chunk.mime_type.startswith("audio/"):
                        await self.send_realtime_input(audio=chunk)
                    elif hasattr(chunk, 'mime_type') and (chunk.mime_type.startswith("image/") or chunk.mime_type.startswith("video/")):
                        await self.send_realtime_input(video=chunk)
                    elif hasattr(chunk, 'mime_type') and chunk.mime_type.startswith("text/"):
                        await self.send_realtime_input(text=chunk.data.decode('utf-8'))
                    else:
                        await self.send_realtime_input(video=chunk)
                return None

        return await _original_send_realtime_input(self, **kwargs)

    live.AsyncSession.send_realtime_input = _patched_send_realtime_input

    # ========================================
    # 2. Patch GeminiLlmConnection
    # ========================================
    _original_send_realtime = GeminiLlmConnection.send_realtime
    _original_send_content = GeminiLlmConnection.send_content

    async def _patched_send_realtime(self, input):
        if isinstance(input, types.Blob):
            if input.mime_type and input.mime_type.startswith("audio/"):
                await self._gemini_session.send_realtime_input(audio=input)
            elif input.mime_type and (input.mime_type.startswith("image/") or input.mime_type.startswith("video/")):
                # Video frames are common, only log occasionally if needed
                await self._gemini_session.send_realtime_input(video=input)
            elif input.mime_type and input.mime_type.startswith("text/"):
                logger.info(f"[PATCH] Sending text blob: {input.data.decode('utf-8')}")
                await self._gemini_session.send_realtime_input(text=input.data.decode('utf-8'))
            else:
                await self._gemini_session.send_realtime_input(audio=input)
        elif isinstance(input, str):
            logger.info(f"[PATCH] Sending text: {input}")
            await self._gemini_session.send_realtime_input(text=input)
        elif isinstance(input, types.Content):
            # Extract text parts from Content
            for part in input.parts:
                if part.text:
                    logger.info(f"[PATCH] Sending content text part: {part.text}")
                    await self._gemini_session.send_realtime_input(text=part.text)
                elif part.inline_data:
                    await self.send_realtime(part.inline_data)
        else:
            if hasattr(input, 'media_chunks') and input.media_chunks:
                for chunk in input.media_chunks:
                    await self.send_realtime(chunk)
                return
            await self._gemini_session.send_realtime_input(video=input)

    async def _patched_send_content(self, content: types.Content):
        # Gemini 3.1: Use send_realtime_input for text instead of send_client_content
        if content.parts:
            # Check for tool responses
            if any(part.function_response for part in content.parts):
                # Tool responses still use send_client_content OR the send(...) wrapper in ADK
                await _original_send_content(self, content)
                return

            # Check for text only parts
            text_parts = [part.text for part in content.parts if part.text]
            if len(text_parts) == len(content.parts):
                logger.info(f"[PATCH] Sending content as text: {text_parts}")
                for text in text_parts:
                    await self._gemini_session.send_realtime_input(text=text)
                return

        # Fallback to original
        await _original_send_content(self, content)

    GeminiLlmConnection.send_realtime = _patched_send_realtime
    GeminiLlmConnection.send_content = _patched_send_content

    # ========================================
    # 3. Patch LiveRequestQueue.send_realtime
    # ========================================
    _original_q_send_realtime = LiveRequestQueue.send_realtime

    def _patched_q_send_realtime(self, blob):
        # We MUST use model_construct if the input is not a Blob to prevent coercion
        # and loss of data by Pydantic's validation loop.
        if isinstance(blob, types.Blob):
            try:
                self._queue.put_nowait(LiveRequest(blob=blob))
            except Exception:
                req = LiveRequest.model_construct(blob=blob)
                self._queue.put_nowait(req)
        else:
            # For strings, Content, etc., always use model_construct
            req = LiveRequest.model_construct(blob=blob)
            self._queue.put_nowait(req)

    LiveRequestQueue.send_realtime = _patched_q_send_realtime

    logger.info("Patches applied successfully.")
