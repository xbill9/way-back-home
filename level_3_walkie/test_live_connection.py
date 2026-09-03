"""Manual smoke check: does this API key actually get a Live session?

BILLED. Excluded from default collection (see pytest.ini); run it by path:

    python -m pytest test_live_connection.py -s
    MODEL_ID=models/clever-chatter python test_live_connection.py

This is the fastest way to prove EAP access, since the only symptom of a
non-allowlisted project is the socket refusing to open. It talks to google-genai
directly -- no ADK, no agent -- so a failure here is about the key and the model,
nothing else.
"""

import asyncio
import os

import pytest
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv("backend/app/biometric_agent/.env")

api_key = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=api_key)

# Matches the project default (models/walkie-talkie). Override to check the
# other EAP endpoint, or the pre-EAP model this replaced.
model_id = os.getenv("MODEL_ID", "models/walkie-talkie").strip("\"'")


@pytest.mark.anyio
@pytest.mark.live
async def test_live():
    print(f"Testing model: {model_id}")
    # No try/except, unlike the other root-level smoke checks: "the EAP model
    # refused this key" is the entire reason to run this, and a check that
    # prints the error and passes anyway cannot tell you that.
    #
    # AUDIO is the only response modality the EAP models support.
    async with client.aio.live.connect(
        model=model_id,
        config=types.LiveConnectConfig(response_modalities=["AUDIO"]),
    ) as session:
        print("Connected successfully!")
        await session.send_realtime_input(text="Neural handshake")
        async for event in session.receive():
            print(f"Received event: {type(event)}")
            break  # Just need one event to confirm connection


if __name__ == "__main__":
    asyncio.run(test_live())
