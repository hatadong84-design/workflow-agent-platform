from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PROJECT_DIR.parent
load_dotenv(PROJECT_DIR / ".env")
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_CHECKPOINT = PROJECT_DIR / "logs" / "workflow_platform_checkpoints.sqlite"

