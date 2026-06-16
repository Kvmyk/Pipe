"""
Handlery dla tool calling agenta.

Moduł zawiera osobne implementacje dla każdego dostępnego narzędzia.
"""

from .command import handle_execute_command
from .file_ops import handle_read_file, handle_write_file
from .git import handle_git_command
from .docker import handle_docker_manage
from .system import (
    handle_change_directory,
    handle_system_stats,
    handle_network_info,
    handle_cron_manage,
)

__all__ = [
    "handle_execute_command",
    "handle_read_file",
    "handle_write_file",
    "handle_git_command",
    "handle_docker_manage",
    "handle_change_directory",
    "handle_system_stats",
    "handle_network_info",
    "handle_cron_manage",
]
