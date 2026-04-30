from dotenv import load_dotenv
import os

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://localhost:8001")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:4200")
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret")
