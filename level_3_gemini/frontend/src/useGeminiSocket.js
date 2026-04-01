import { useState, useRef, useCallback, useEffect } from 'react';
import { AudioStreamer } from './audioStreamer';
import { AudioRecorder } from './audioRecorder';

export function useGeminiSocket(url, { onDigitDetected, onSystemError } = {}) {
    const [status, setStatus] = useState('DISCONNECTED');
    const [lastMessage, setLastMessage] = useState(null);
    const [isMock, setIsMock] = useState(false);

    const onDigitDetectedRef = useRef(onDigitDetected);
    const onSystemErrorRef = useRef(onSystemError);
    useEffect(() => {
        onDigitDetectedRef.current = onDigitDetected;
        onSystemErrorRef.current = onSystemError;
    }, [onDigitDetected, onSystemError]);

    const ws = useRef(null);
    const streamRef = useRef(null);
    const intervalRef = useRef(null);
    const audioStreamer = useRef(new AudioStreamer(24000)); // Default to 24kHz for Gemini Live
    const audioRecorder = useRef(new AudioRecorder(16000)); // Record at 16kHz for Gemini Input

    const stopStream = useCallback(() => {
        // Stop Video
        if (streamRef.current) {
            streamRef.current.getTracks().forEach(track => track.stop());
            streamRef.current = null;
        }
        // Stop Audio
        audioRecorder.current.stop();

        // Clear Interval
        if (intervalRef.current) {
            clearInterval(intervalRef.current);
            intervalRef.current = null;
        }
    }, []);

    const connect = useCallback(() => {
        if (ws.current?.readyState === WebSocket.OPEN) return;

        ws.current = new WebSocket(url);

        ws.current.onopen = () => {
            console.log('Connected to Gemini Socket');
            setStatus('CONNECTED');
        };

        ws.current.onclose = () => {
            console.log('Disconnected from Gemini Socket');
            setStatus('DISCONNECTED');
            stopStream();
        };

        ws.current.onerror = (err) => {
            console.error('Socket error:', err);
            setStatus('ERROR');
        };

        ws.current.onmessage = async (event) => {
            try {
                // console.log("Raw WS Frame:", event.data.slice(0, 200)); 
                const msg = JSON.parse(event.data);
                // console.log("[useGeminiSocket] Received message from backend:", msg);

                // Detect mock server identification flag
                if (msg.mock === true) {
                    setIsMock(true);
                    return;
                }

                // Handle direct "match" message from backend
                if (msg.type === 'match') {
                    const count = msg.count || msg.digit;
                    if (count !== undefined) {
                        const val = parseInt(count, 10);
                        console.log(`[DEBUG] MATCH SIGNAL FROM BACKEND: ${val}`);
                        setLastMessage({ type: 'DIGIT_DETECTED', value: val });
                        if (onDigitDetectedRef.current) onDigitDetectedRef.current(val);
                    }
                    return; // Skip further processing for this specific message
                }

                // Handle direct "system_error" message from backend
                if (msg.type === 'system_error') {
                    console.log(`[DEBUG] SYSTEM ERROR FROM BACKEND: ${msg.message}`);
                    setLastMessage({ type: 'SYSTEM_ERROR', message: msg.message });
                    if (onSystemErrorRef.current) onSystemErrorRef.current(msg.message);
                    return;
                }

                // Helper to extract parts from various possible event structures
                let parts = [];
                if (msg.serverContent?.modelTurn?.parts) {
                    parts = msg.serverContent.modelTurn.parts;
                } else if (msg.content?.parts) {
                    parts = msg.content.parts;
                }

                if (parts.length > 0) {
                    // console.log(`[useGeminiSocket] Processing ${parts.length} parts`);
                    parts.forEach(part => {
                        // Handle Tool Calls
                        if (part.functionCall) {
                            console.log('[DEBUG] Tool Call Detected:', part.functionCall);
                            if (part.functionCall.name === 'report_digit') {
                                // Agent uses 'count', check both for safety
                                const countStr = part.functionCall.args.count || part.functionCall.args.digit;
                                const count = parseInt(countStr, 10);
                                if (!isNaN(count)) {
                                    console.log(`[DEBUG] DIGIT DETECTED (via Tool Call): ${count}`);
                                    setLastMessage({ type: 'DIGIT_DETECTED', value: count });
                                    if (onDigitDetectedRef.current) onDigitDetectedRef.current(count);
                                }
                            }
                        }

                        // Handle Audio (inlineData)
                        if (part.inlineData && part.inlineData.data) {
                            // console.log(`[useGeminiSocket] Found inlineData: ${part.inlineData.data.length} chars`);
                            // Resume context if needed (autoplay policy)
                            audioStreamer.current.resume();
                            audioStreamer.current.addPCM16(part.inlineData.data);
                        }

                        // Handle Text (transcript)
                        if (part.text) {
                            console.log(`[DEBUG] Gemini said: ${part.text}`);
                        }
                    });
                }
            } catch (e) {
                console.error('Failed to parse message', e, event.data.slice(0, 100));
            }
        };
    }, [url, stopStream]);

    const startStream = useCallback(async (videoElement) => {
        try {
            console.log("[DEBUG] Starting stream...");
            // 1. Start Video Stream
            const stream = await navigator.mediaDevices.getUserMedia({ video: true });
            videoElement.srcObject = stream;
            streamRef.current = stream;
            await videoElement.play();
            console.log("[DEBUG] Video stream started");

            // 2. Start Audio Recording (Microphone)
            try {
                let packetCount = 0;
                await audioRecorder.current.start((pcmBuffer) => {
                    if (ws.current?.readyState === WebSocket.OPEN) {
                        packetCount++;
                        if (packetCount % 50 === 0) console.log(`[useGeminiSocket] Sending BINARY Audio Packet #${packetCount}, size: ${pcmBuffer.byteLength}`);
                        // Send as raw binary frame for lowest overhead
                        ws.current.send(pcmBuffer);
                    } else {
                        if (packetCount % 50 === 0) console.warn('[useGeminiSocket] WS not OPEN, cannot send audio');
                    }
                });
                console.log("[DEBUG] Microphone recording started (BINARY MODE)");
            } catch (authErr) {
                console.error("Microphone access denied or error:", authErr);
            }

            // 3. Setup Video Frame Capture loop
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d');
            const width = 640;
            const height = 480;
            canvas.width = width;
            canvas.height = height;

            let frameCount = 0;
            intervalRef.current = setInterval(() => {
                if (ws.current?.readyState === WebSocket.OPEN) {
                    ctx.drawImage(videoElement, 0, 0, width, height);
                    const base64 = canvas.toDataURL('image/jpeg', 0.6).split(',')[1];
                    frameCount++;
                    if (frameCount % 10 === 0) {
                        console.log(`[DEBUG] Sending image frame #${frameCount}, size: ${base64.length}`);
                    }
                    // ADK format: { type: "image", data: base64, mimeType: "image/jpeg" }
                    ws.current.send(JSON.stringify({
                        type: 'image',
                        data: base64,
                        mimeType: 'image/jpeg'
                    }));
                }
            }, 500); // 2 FPS (Constraint: Maintain 2 FPS)

        } catch (err) {
            console.error('Error accessing camera:', err);
        }
    }, []);

    useEffect(() => {
        return () => {
            stopStream();
            if (ws.current) ws.current.close();
        };
    }, [stopStream]);

    const disconnect = useCallback(() => {
        if (ws.current) {
            ws.current.close();
            ws.current = null;
        }
        setStatus('DISCONNECTED');
        stopStream();
    }, [stopStream]);

    return { status, lastMessage, isMock, connect, disconnect, startStream, stopStream };
}

