import pytest
from backend.core.tools import TOOLS

def test_tools_list_contains_expected_functions():
    tool_names = [tool["function"]["name"] for tool in TOOLS]
    
    assert "execute_command" in tool_names
    assert "read_file" in tool_names
    assert "write_file" in tool_names
    assert "change_directory" in tool_names
    assert "git_command" in tool_names
    assert "system_stats" in tool_names
    assert "docker_manage" in tool_names

def test_execute_command_tool_structure():
    cmd_tool = next(t for t in TOOLS if t["function"]["name"] == "execute_command")
    props = cmd_tool["function"]["parameters"]["properties"]
    assert "command" in props
    assert "requires_confirmation" in props

