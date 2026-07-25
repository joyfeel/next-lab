import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".cache" / "state.json"


def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass
class Config:
    gemini_api_key: str
    data_api_key: str
    telegram_bot_token: str
    telegram_chat_ids: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        return cls(
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            data_api_key=os.environ.get("DATA_API_KEY", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            # Comma-separated: personal chat + any group chat_ids, e.g.
            # "1257249563,-1001234567890"
            telegram_chat_ids=[
                c.strip()
                for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",")
                if c.strip()
            ],
            authors=[
                a.strip()
                for a in os.environ.get("WATCH_AUTHORS", "lotterywin").split(",")
                if a.strip()
            ],
        )

    def missing_keys(self) -> list[str]:
        missing = []
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if not self.data_api_key:
            missing.append("DATA_API_KEY")
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_ids:
            missing.append("TELEGRAM_CHAT_ID")
        return missing
