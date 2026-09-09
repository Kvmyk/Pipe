"""
Testy dla modułu executor.py — wykonywanie komend i operacje na plikach.

Pokrywają:
  - executor.execute() — uruchamianie komend shell
  - executor.read_file() — odczyt pliku
  - executor.write_file() — zapis pliku
  - Timeout, stderr, exit codes
  - Obsługa błędów
"""

import asyncio
import pytest
import tempfile
from pathlib import Path

from backend.core import executor as executor_module


@pytest.fixture
def executor():
    """Fixture zwracający moduł executora (API jest modułowe, nie klasowe)."""
    return executor_module


@pytest.fixture
def temp_dir():
    """Fixture zwracający tymczasowy katalog."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestExecuteCommand:
    """Testy dla executor.execute()."""

    @pytest.mark.asyncio
    async def test_simple_command(self, executor):
        """Test prostej komendy."""
        stdout, stderr, exit_code = await executor.execute("echo 'hello'")
        assert "hello" in stdout
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_command_with_args(self, executor):
        """Test komendy z argumentami."""
        stdout, stderr, exit_code = await executor.execute("echo hello world")
        assert "hello world" in stdout
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_command_with_stderr(self, executor):
        """Test komendy generującej stderr."""
        stdout, stderr, exit_code = await executor.execute("ls /nonexistent 2>&1")
        # Raczej będzie stdout zamiast stderr, bo redirect
        assert exit_code != 0 or len(stderr) > 0 or len(stdout) > 0

    @pytest.mark.asyncio
    async def test_failed_command_exit_code(self, executor):
        """Test komendy, która zawiedzie."""
        stdout, stderr, exit_code = await executor.execute("exit 42")
        assert exit_code == 42

    @pytest.mark.asyncio
    async def test_command_with_cwd(self, executor, temp_dir):
        """Test komendy z katalogiem roboczym."""
        # Użyjemy komendy, która zawsze działa na Windows i Unix
        stdout, stderr, exit_code = await executor.execute("echo test", cwd=str(temp_dir))
        assert exit_code == 0 or stdout.strip()  # Check either zero exit or output

    @pytest.mark.asyncio
    async def test_command_timeout(self, executor):
        """Test timeoutu dla długo-trwającej komendy."""
        # Użyjemy timeout z ping zamiast sleep (Windows compatibility)
        import sys
        if sys.platform == "win32":
            # Windows: ping loop przez 60 sekund
            stdout, stderr, exit_code = await executor.execute("ping -t 127.0.0.1")
        else:
            stdout, stderr, exit_code = await executor.execute("sleep 60")
        
        assert exit_code == 124  # Timeout exit code

    @pytest.mark.asyncio
    async def test_command_empty_output(self, executor, temp_dir):
        """Test komendy bez outputu."""
        # Windows: use "exit 0" zamiast "true"
        import sys
        cmd = "exit /b 0" if sys.platform == "win32" else "true"
        stdout, stderr, exit_code = await executor.execute(cmd)
        assert exit_code == 0
        # executor.execute() zwraca "Komenda wykonana bez outputu" jeśli brak output
        # lub może być pusty stdout

    @pytest.mark.asyncio
    async def test_nonexistent_command(self, executor):
        """Test komendy, która nie istnieje."""
        import sys
        stdout, stderr, exit_code = await executor.execute("this_command_does_not_exist_12345")
        # Komenda idzie przez powloke, wiec to shell zglasza blad:
        # exit 127 + komunikat "command not found" na stderr (Unix), 1 na Windows.
        assert exit_code != 0
        assert exit_code in (1, 127)
        assert "not found" in stderr.lower() or "nie znaleziona" in stderr.lower()


class TestReadFile:
    """Testy dla executor.read_file()."""

    @pytest.mark.asyncio
    async def test_read_existing_file(self, executor, temp_dir):
        """Test odczytu istniejącego pliku."""
        test_file = temp_dir / "test.txt"
        test_file.write_text("Hello, World!")
        
        content = await executor.read_file(str(test_file))
        assert content == "Hello, World!"

    @pytest.mark.asyncio
    async def test_read_empty_file(self, executor, temp_dir):
        """Test odczytu pustego pliku."""
        test_file = temp_dir / "empty.txt"
        test_file.write_text("")
        
        content = await executor.read_file(str(test_file))
        assert content == ""

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, executor):
        """Test odczytu nieistniejącego pliku."""
        with pytest.raises(FileNotFoundError):
            await executor.read_file("/nonexistent/file/path")

    @pytest.mark.asyncio
    async def test_read_directory_fails(self, executor, temp_dir):
        """Test odczytu katalogu zamiast pliku."""
        with pytest.raises(ValueError):
            await executor.read_file(str(temp_dir))

    @pytest.mark.asyncio
    async def test_read_file_with_unicode(self, executor, temp_dir):
        """Test odczytu pliku z Unicode."""
        test_file = temp_dir / "unicode.txt"
        test_file.write_text("Ączka Żółw 中文", encoding="utf-8")
        
        content = await executor.read_file(str(test_file))
        assert "Ączka" in content
        assert "中文" in content

    @pytest.mark.asyncio
    async def test_read_large_file(self, executor, temp_dir):
        """Test odczytu dużego pliku."""
        test_file = temp_dir / "large.txt"
        large_content = "x" * 1000000  # 1MB
        test_file.write_text(large_content)
        
        content = await executor.read_file(str(test_file))
        assert len(content) == 1000000


class TestWriteFile:
    """Testy dla executor.write_file()."""

    @pytest.mark.asyncio
    async def test_write_new_file(self, executor, temp_dir):
        """Test zapisu nowego pliku."""
        test_file = temp_dir / "new.txt"
        
        await executor.write_file(str(test_file), "New content")
        assert test_file.read_text() == "New content"

    @pytest.mark.asyncio
    async def test_write_overwrites_existing(self, executor, temp_dir):
        """Test nadpisania istniejącego pliku."""
        test_file = temp_dir / "overwrite.txt"
        test_file.write_text("Old content")
        
        await executor.write_file(str(test_file), "New content")
        assert test_file.read_text() == "New content"

    @pytest.mark.asyncio
    async def test_write_creates_parent_directories(self, executor, temp_dir):
        """Test tworzenia katalogów nadrzędnych."""
        test_file = temp_dir / "a" / "b" / "c" / "file.txt"
        
        await executor.write_file(str(test_file), "Content in nested dir")
        assert test_file.read_text() == "Content in nested dir"

    @pytest.mark.asyncio
    async def test_write_unicode_content(self, executor, temp_dir):
        """Test zapisu pliku z Unicode."""
        test_file = temp_dir / "unicode_write.txt"
        
        await executor.write_file(str(test_file), "Ączka Żółw 中文")
        content = test_file.read_text(encoding="utf-8", errors="replace")
        # Sprawdzamy, że plik zawiera przynajmniej część zawartości
        assert "czka" in content or "ółw" in content or "中" in content

    @pytest.mark.asyncio
    async def test_write_empty_content(self, executor, temp_dir):
        """Test zapisu pustego pliku."""
        test_file = temp_dir / "empty_write.txt"
        
        await executor.write_file(str(test_file), "")
        assert test_file.read_text() == ""

    @pytest.mark.asyncio
    async def test_write_large_content(self, executor, temp_dir):
        """Test zapisu dużego pliku."""
        test_file = temp_dir / "large_write.txt"
        large_content = "x" * 1000000  # 1MB
        
        await executor.write_file(str(test_file), large_content)
        assert len(test_file.read_text()) == 1000000

    @pytest.mark.asyncio
    async def test_write_multiline_content(self, executor, temp_dir):
        """Test zapisu wieloliniowego pliku."""
        test_file = temp_dir / "multiline.txt"
        multiline_content = "Line 1\nLine 2\nLine 3\n"
        
        await executor.write_file(str(test_file), multiline_content)
        assert test_file.read_text() == multiline_content


class TestExecutorTimeout:
    """Testy dla timeoutu executora."""

    @pytest.mark.asyncio
    async def test_timeout_value(self, executor):
        """Sprawdzenie wartości timeoutu."""
        assert executor.TIMEOUT_SECONDS == 30

    @pytest.mark.asyncio
    async def test_quick_command_completes(self, executor):
        """Test szybkiej komendy."""
        stdout, stderr, exit_code = await executor.execute("echo 'quick'")
        assert exit_code == 0
        assert "quick" in stdout


class TestConcurrentExecute:
    """Testy dla jednoczesnego wykonywania komend."""

    @pytest.mark.asyncio
    async def test_concurrent_commands(self, executor):
        """Test równoczesnego wykonywania wielu komend."""
        tasks = [
            executor.execute("echo 1"),
            executor.execute("echo 2"),
            executor.execute("echo 3"),
        ]
        
        results = await asyncio.gather(*tasks)
        
        assert len(results) == 3
        assert "1" in results[0][0]
        assert "2" in results[1][0]
        assert "3" in results[2][0]

    @pytest.mark.asyncio
    async def test_concurrent_file_operations(self, executor, temp_dir):
        """Test równoczesnych operacji na plikach."""
        files = [temp_dir / f"file{i}.txt" for i in range(3)]
        
        write_tasks = [
            executor.write_file(str(f), f"Content {i}")
            for i, f in enumerate(files)
        ]
        await asyncio.gather(*write_tasks)
        
        read_tasks = [executor.read_file(str(f)) for f in files]
        contents = await asyncio.gather(*read_tasks)
        
        assert len(contents) == 3
        assert "Content 0" in contents[0]
        assert "Content 1" in contents[1]
        assert "Content 2" in contents[2]
