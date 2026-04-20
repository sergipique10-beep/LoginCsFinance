from dotenv import load_dotenv
import os

load_dotenv()

# Your public URL (use ngrok or similar for local testing)
BASE_URL = os.getenv("BASE_URL", "http://localhost:8001")

# Optional: get it at https://steamcommunity.com/dev/apikey
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
