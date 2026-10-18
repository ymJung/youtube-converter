import os
from pathlib import Path
from dotenv import load_dotenv
import imageio_ffmpeg

# Load ~/.env first, then project .env if present
HOME_ENV_PATH = Path.home() / ".env"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_ENV_PATH = PROJECT_ROOT / ".env"
BIN_DIR = PROJECT_ROOT / "bin"

if HOME_ENV_PATH.exists():
    load_dotenv(HOME_ENV_PATH, override=False)
if LOCAL_ENV_PATH.exists():
    load_dotenv(LOCAL_ENV_PATH, override=True)

# Ensure local bin directory (containing deno & ffmpeg symlink) is in PATH
if BIN_DIR.exists():
    current_path = os.environ.get("PATH", "")
    if str(BIN_DIR) not in current_path:
        os.environ["PATH"] = f"{BIN_DIR}:{current_path}"


class Settings:
    PROJECT_NAME: str = "TubeShift (유튜브 MP3 · MP4 오픈소스 변환 서버)"
    VERSION: str = "1.0.0"
    DB_PATH: Path = PROJECT_ROOT / "data" / "youtube_converter.db"
    DOWNLOADS_DIR: Path = PROJECT_ROOT / "downloads"

    # Docker MySQL Configuration
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "127.0.0.1").strip()
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "devuser").strip()
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "devpass123!").strip()
    MYSQL_DATABASE: str = os.getenv("MYSQL_DATABASE", "youtube_converter").strip()

    # Server Protocol (Port 8010 to avoid conflict with AuctionRadar on Port 8000)
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8010"))

    # Telegram Configuration from ~/.env (uses TELEGRAM_BOT_3 @soracrabsss_bot)
    TELEGRAM_USER_ID: str = os.getenv("TELEGRAM_USER_ID", "").strip()
    TELEGRAM_BOT_1: str = os.getenv("TELEGRAM_BOT_1", "").strip().replace(" ", "")
    TELEGRAM_BOT_2: str = os.getenv("TELEGRAM_BOT_2", "").strip().replace(" ", "")
    TELEGRAM_BOT_3: str = os.getenv("TELEGRAM_BOT_3", "").strip().replace(" ", "")

    @property
    def ffmpeg_path(self) -> str:
        """Return static FFmpeg binary path (local symlink or imageio_ffmpeg)."""
        local_ffmpeg = BIN_DIR / "ffmpeg"
        if local_ffmpeg.exists():
            return str(local_ffmpeg)
        return imageio_ffmpeg.get_ffmpeg_exe()

    @property
    def active_telegram_token(self) -> str:
        """Return TELEGRAM_BOT_3 as primary token for youtube-converter."""
        if HOME_ENV_PATH.exists():
            load_dotenv(HOME_ENV_PATH, override=True)
        bot3 = os.getenv("TELEGRAM_BOT_3", "").strip().replace(" ", "")
        if bot3 and ":" in bot3:
            return bot3
        return ""


settings = Settings()
settings.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
