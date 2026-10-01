"""
Handlery dla tool calling agenta.

Moduł zawiera osobne implementacje dla każdego dostępnego narzędzia.
Dispatch w agent.py szuka tu funkcji `handle_<nazwa_narzedzia>`.
"""

from .audit import handle_security_audit
from .command import handle_execute_command
from .file_ops import handle_read_file, handle_write_file
from .git import handle_git_command
from .docker import handle_docker_manage
from .memory import handle_directory, handle_server_md, handle_skill_manage, handle_vibe
from .diagram import handle_diagram
from .history import handle_server_history
from .journal import handle_journal
from .mcp import handle_mcp_manage
from .remote import handle_delegate, handle_remote_exec, handle_target_manage
from .routines import handle_routine_manage
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
    "handle_server_md",
    "handle_skill_manage",
    "handle_directory",
    "handle_vibe",
    "handle_diagram",
    "handle_target_manage",
    "handle_remote_exec",
    "handle_delegate",
    "handle_routine_manage",
    "handle_server_history",
    "handle_journal",
    "handle_security_audit",
    "handle_mcp_manage",
]
