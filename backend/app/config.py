import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
USE_LLM = os.getenv("USE_LLM", "false").lower() == "true"
SIMULATOR_SPEED = float(os.getenv("SIMULATOR_SPEED", "20"))  # seconds between major game events
SIMULATOR_EVENT_DELAY = float(os.getenv("SIMULATOR_EVENT_DELAY", "6"))  # seconds between cards within a single game tick
