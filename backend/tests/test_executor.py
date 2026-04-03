import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from backend.core.executor import LocalExecutor

@pytest.mark.asyncio
async def test_execute_command():
    with patch("asyncio.create_subprocess_shell") as mock_subproc:
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output\n", b""))
        mock_process.returncode = 0
        mock_subproc.return_value = mock_process
        
        executor = LocalExecutor()
        stdout, stderr, exit_code = await executor.execute("ls")
        assert stdout == "output\n"
        assert stderr == ""
        assert exit_code == 0

@pytest.mark.asyncio
async def test_read_file_safe():
    with patch("pathlib.Path.read_text") as mock_read, \
         patch("pathlib.Path.exists") as mock_exists, \
         patch("pathlib.Path.is_file") as mock_is_file:
        mock_exists.return_value = True
        mock_is_file.return_value = True
        mock_read.return_value = "file content"
        
        executor = LocalExecutor()
        result = await executor.read_file("/tmp/safe.txt")
        assert result == "file content"
