import os
from google import genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv("biometric_agent/.env")

api_key = os.getenv("GOOGLE_API_KEY")
if not api_key:
    api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    print("Error: GOOGLE_API_KEY or GEMINI_API_KEY not found in environment.")
    exit(1)

client = genai.Client(api_key=api_key)

print(f"{'Model Name':<50} | {'Supports Live API'}")
print("-" * 70)

for model in client.models.list():
    # In the current genai SDK, we check for 'bidiGenerateContent' in supported_actions
    # or look for specific model families known to support it (like gemini-2.0 and native-audio)
    supports_live = False

    # Check supported_actions if available
    if hasattr(model, "supported_actions"):
        if "bidiGenerateContent" in model.supported_actions:
            supports_live = True

    # Fallback/Additional check based on known model names if the SDK attribute isn't populated as expected
    if not supports_live:
        name_lower = model.name.lower()
        if (
            "live" in name_lower
            or "gemini-2.0" in name_lower
            or "native-audio" in name_lower
        ):
            supports_live = True

    status = "YES" if supports_live else "no"
    print(f"{model.name:<50} | {status}")
