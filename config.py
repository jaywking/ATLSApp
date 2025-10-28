from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


class Config:
    """
    Centralised configuration helper used by the legacy CLI scripts.

    Most scripts call `Config.setup()` once at launch to load `.env` values and
    populate the class attributes below. The NiceGUI adapters rely on the same
    module so the behaviour needs to stay identical to the original toolkit.
    """

    _is_loaded: bool = False

    # Core secrets / API access
    NOTION_TOKEN: Optional[str] = None
    GOOGLE_MAPS_API_KEY: Optional[str] = None

    # Notion database identifiers
    PRODUCTIONS_DB_ID: Optional[str] = None
    PRODUCTIONS_MASTER_DB: Optional[str] = None
    LOCATIONS_MASTER_DB: Optional[str] = None
    MEDICAL_FACILITIES_DB: Optional[str] = None
    NOTION_DATABASES_PARENT_PAGE_ID: Optional[str] = None

    # Optional metadata used by the scripts
    NOTION_DATABASE_NAME: Optional[str] = None  # populated at runtime

    # Status field defaults
    STATUS_ON_RESET: str = "Ready"
    STATUS_AFTER_MATCHING: str = "Matched"
    STATUS_ERROR: str = "Error"

    # Property name overrides for Notion schemas
    PROD_MASTER_LINK_PROP: Optional[str] = "Locations Table"
    PROD_MASTER_ABBR_PROP: Optional[str] = "Abbreviation"
    PROD_MASTER_TITLE_PROP: Optional[str] = "ProductionID"

    # Optional database connection details (currently unused but defined upstream)
    DB_HOST: Optional[str] = None
    DB_PORT: Optional[str] = None
    DB_NAME: Optional[str] = None
    DB_USER: Optional[str] = None
    DB_PASSWORD: Optional[str] = None

    @classmethod
    def setup(cls, env_path: Optional[Path] = None, *, force: bool = False) -> None:
        """
        Load environment variables into class attributes.

        Args:
            env_path: Optional path to a `.env` file. Defaults to project root.
            force: Reload values even if setup has already run.
        """
        if cls._is_loaded and not force:
            return

        # Default `.env` lives at the repository root (one level up from this file)
        if env_path is None:
            env_path = Path(__file__).resolve().parent / ".env"

        # Load .env if present; fallback to existing environment variables otherwise
        load_dotenv(dotenv_path=env_path, override=True)

        cls.NOTION_TOKEN = os.getenv("NOTION_TOKEN")
        cls.GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

        cls.PRODUCTIONS_DB_ID = os.getenv("PRODUCTIONS_DB_ID")
        cls.PRODUCTIONS_MASTER_DB = os.getenv("PRODUCTIONS_MASTER_DB")
        cls.LOCATIONS_MASTER_DB = os.getenv("LOCATIONS_MASTER_DB")
        cls.MEDICAL_FACILITIES_DB = os.getenv("MEDICAL_FACILITIES_DB")
        cls.NOTION_DATABASES_PARENT_PAGE_ID = os.getenv("NOTION_DATABASES_PARENT_PAGE_ID")

        cls.STATUS_ON_RESET = os.getenv("STATUS_ON_RESET", cls.STATUS_ON_RESET)
        cls.STATUS_AFTER_MATCHING = os.getenv("STATUS_AFTER_MATCHING", cls.STATUS_AFTER_MATCHING)
        cls.STATUS_ERROR = os.getenv("STATUS_ERROR", cls.STATUS_ERROR)

        cls.PROD_MASTER_LINK_PROP = os.getenv("PROD_MASTER_LINK_PROP", cls.PROD_MASTER_LINK_PROP)
        cls.PROD_MASTER_ABBR_PROP = os.getenv("PROD_MASTER_ABBR_PROP", cls.PROD_MASTER_ABBR_PROP)
        cls.PROD_MASTER_TITLE_PROP = os.getenv("PROD_MASTER_TITLE_PROP", cls.PROD_MASTER_TITLE_PROP)

        cls.DB_HOST = os.getenv("DB_HOST")
        cls.DB_PORT = os.getenv("DB_PORT")
        cls.DB_NAME = os.getenv("DB_NAME")
        cls.DB_USER = os.getenv("DB_USER")
        cls.DB_PASSWORD = os.getenv("DB_PASSWORD")

        cls._is_loaded = True


# Allow modules that import Config at import-time to have sensible defaults.
Config.setup()
