import os
import asyncio
import pytest
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv("backend/app/biometric_agent/.env")

api_key = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=api_key)

# gemini-3.1-flash-live-preview is the default in this project
model_id = "gemini-3.1-flash-live-preview"


@pytest.mark.anyio
async def test_live():
    print(f"Testing model: {model_id}")
    try:
        # Gemini 3.1 Live API requires Async for some operations in this SDK version
        async with client.aio.live.connect(
            model=model_id,
            config=types.LiveConnectConfig(response_modalities=["AUDIO"]),
        ) as session:
            print("Connected successfully!")
            # Send a tiny silent audio chunk or text to verify it's working
            await session.send_realtime_input(text="Neural handshake")
            async for event in session.receive():
                print(f"Received event: {type(event)}")
                break  # Just need one event to confirm connection
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_live())
