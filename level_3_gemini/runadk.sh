cd $HOME/way-back-home/level_3_gemini/backend/app/biometric_agent
echo "GOOGLE_CLOUD_PROJECT=$(cat ~/project_id.txt)" > .env
echo "GOOGLE_CLOUD_LOCATION=us-central1" >> .env
echo "GOOGLE_GENAI_USE_VERTEXAI=False" >> .env
echo "GOOGLE_API_KEY=$GOOGLE_API_KEY" >> .env
echo "GEMINI_API_KEY=$GOOGLE_API_KEY" >> .env
echo "GEMINI_KEY=$GOOGLE_API_KEY" >> .env
echo "MODEL_ID=gemini-3.1-flash-live-preview" >> .env

cd $HOME/way-back-home/level_3_gemini/backend/app

echo 'connect on http://127.0.0.1:8080/'
echo
python main.py
