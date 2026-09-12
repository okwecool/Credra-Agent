"""Repository-root Chainlit entry point."""

from app.chainlit_app import *
from app.config import Settings
from credra_agent.observability.runtime import (
    config_from_settings,
    start_process_service,
)

start_process_service("chainlit", config_from_settings(Settings()))
