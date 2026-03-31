import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv('backend/app/biometric_agent/.env')

api_key = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=api_key)

# Try with models/ prefix
model_id = "models/gemini-2.5-flash-native-audio-latest"

print(f"Testing model: {model_id}")

try:
    # Use client.models.generate_content_stream as a proxy for checking if the model exists and is accessible
    # since client.live.connect seemed missing (maybe it's under a different path in this version)
    # Actually, let's try to find where live connect is.
    
    # In some versions it might be client.models.live
    if hasattr(client.models, 'live'):
        print("Found client.models.live")
    
    # Let's try to just call it and see the error
    with client.live.connect(model=model_id, config=types.LiveConnectConfig(response_modalities=["AUDIO"])) as session:
        print("Connected successfully!")
except Exception as e:
    print(f"Error: {e}")
