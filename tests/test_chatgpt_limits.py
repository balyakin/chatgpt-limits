import argparse
import ast
import asyncio
import io
import json
import logging
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Dict, List, Optional, Sequence, Set, Tuple
from unittest.mock import Mock

import pytest

import chatgpt_limits as limits
from chatgpt_limits import AccountConfig, AccountStatus, AppConfig, AppServerError, ConfigError, LimitWindow
from chatgpt_limits import LoginError, ProtocolError, build_codex_environment, build_initialize_request
from chatgpt_limits import build_initialized_notification, build_rate_limits_request, load_config, login_account
from chatgpt_limits import parse_limit_window, parse_limit_windows, prepare_account_home, read_account_limits
from chatgpt_limits import read_process_stderr, read_rpc_result, render_screen, stop_process, write_rpc_message


class FakeStdin:
    def __init__(self) -> None:
        self.chunks: List[bytes] = []
        self.drain_calls: int = 0

    def write(self, value: bytes) -> None:
        self.chunks.append(value)

    async def drain(self) -> None:
        self.drain_calls += 1


class BrokenStdin(FakeStdin):
    def write(self, value: bytes) -> None:
        raise OSError("synthetic write failure")


class FakeStdout:
    def __init__(
        self,
        lines: Sequence[bytes],
        on_read: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.lines: List[bytes] = list(lines)
        self.on_read: Optional[Callable[[int], None]] = on_read
        self.read_count: int = 0

    async def readline(self) -> bytes:
        if self.on_read is not None:
            self.on_read(self.read_count)
        if self.read_count >= len(self.lines):
            self.read_count += 1
            return b""
        line: bytes = self.lines[self.read_count]
        self.read_count += 1
        return line


class NeverStdout:
    def __init__(self) -> None:
        self.started: asyncio.Event = asyncio.Event()

    async def readline(self) -> bytes:
        self.started.set()
        blocker: asyncio.Event = asyncio.Event()
        await blocker.wait()
        return b""


class FakeProcess:
    def __init__(
        self,
        stdin: Optional[FakeStdin] = None,
        stdout: Optional[object] = None,
        wait_code: int = 0,
        on_wait: Optional[Callable[[], None]] = None,
        never_exit: bool = False,
    ) -> None:
        self.stdin: Optional[FakeStdin] = stdin
        self.stdout: Optional[object] = stdout
        self.wait_code: int = wait_code
        self.on_wait: Optional[Callable[[], None]] = on_wait
        self.never_exit: bool = never_exit
        self.returncode: Optional[int] = None
        self.wait_calls: int = 0
        self.terminate_calls: int = 0
        self.kill_calls: int = 0
        self.killed: bool = False

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.on_wait is not None:
            self.on_wait()
        if self.never_exit and not self.killed:
            blocker: asyncio.Event = asyncio.Event()
            await blocker.wait()
        self.returncode = self.wait_code
        return self.wait_code

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.killed = True


class FakeTerminal:
    def __init__(self, is_tty: bool) -> None:
        self.is_tty: bool = is_tty
        self.values: List[str] = []
        self.flushed: bool = False

    def isatty(self) -> bool:
        return self.is_tty

    def write(self, value: str) -> int:
        self.values.append(value)
        return len(value)

    def flush(self) -> None:
        self.flushed = True

    def text(self) -> str:
        return "".join(self.values)


class StopMonitor(Exception):
    pass


def rpc_line(message: Dict[str, object]) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


def written_messages(stdin: FakeStdin) -> List[Dict[str, object]]:
    payload: bytes = b"".join(stdin.chunks)
    return [json.loads(line.decode("utf-8")) for line in payload.splitlines()]


def make_account(root: Path, slug: str = "personal", name: str = "Personal Pro") -> AccountConfig:
    return AccountConfig(
        slug=slug,
        name=name,
        codex_home=root / "accounts" / slug,
    )


def set_state_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Tuple[Path, Path]:
    state_root: Path = tmp_path / "state"
    accounts_root: Path = state_root / "accounts"
    monkeypatch.setattr(limits, "STATE_ROOT", state_root)
    monkeypatch.setattr(limits, "ACCOUNTS_ROOT", accounts_root)
    monkeypatch.setattr(limits, "LOG_PATH", state_root / "app.log")
    return state_root, accounts_root


def test_load_config_accepts_two_trimmed_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a valid two-account configuration"""

    # ARRANGE
    state_root, accounts_root = set_state_roots(monkeypatch, tmp_path)
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        'refresh_seconds = 300\n\n[[accounts]]\nslug = " personal "\nname = " Personal Pro "\n\n'
        '[[accounts]]\nslug = "work"\nname = "Work Pro"\n',
        encoding="utf-8",
    )
    expected: AppConfig = AppConfig(
        refresh_seconds=300,
        accounts=[
            AccountConfig("personal", "Personal Pro", accounts_root / "personal"),
            AccountConfig("work", "Work Pro", accounts_root / "work"),
        ],
    )

    # ACT
    actual: AppConfig = load_config(config_path)

    # ASSERT
    assert actual == expected
    assert actual.accounts[0].codex_home != actual.accounts[1].codex_home
    assert state_root == accounts_root.parent


def test_load_config_adds_third_account_without_code_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify configuration-driven account expansion"""

    # ARRANGE
    _, accounts_root = set_state_roots(monkeypatch, tmp_path)
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        'refresh_seconds = 90\n\n[[accounts]]\nslug = "personal"\nname = "Personal"\n\n'
        '[[accounts]]\nslug = "work"\nname = "Work"\n\n'
        '[[accounts]]\nslug = "third"\nname = "Third"\n',
        encoding="utf-8",
    )
    expected: AppConfig = AppConfig(
        refresh_seconds=90,
        accounts=[
            AccountConfig("personal", "Personal", accounts_root / "personal"),
            AccountConfig("work", "Work", accounts_root / "work"),
            AccountConfig("third", "Third", accounts_root / "third"),
        ],
    )

    # ACT
    actual: AppConfig = load_config(config_path)

    # ASSERT
    assert actual == expected


def test_load_config_rejects_empty_accounts(tmp_path: Path) -> None:
    """Verify that an empty account array is rejected"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text("refresh_seconds = 300\naccounts = []\n", encoding="utf-8")

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "accounts must be a non-empty array"


def test_load_config_rejects_duplicate_slug(tmp_path: Path) -> None:
    """Verify duplicate slugs after trimming are rejected"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        'refresh_seconds = 300\n[[accounts]]\nslug = "same"\nname = "One"\n'
        '[[accounts]]\nslug = " same "\nname = "Two"\n',
        encoding="utf-8",
    )

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "Duplicate account slug: same"


def test_load_config_rejects_duplicate_trimmed_name(tmp_path: Path) -> None:
    """Verify duplicate display names after trimming are rejected"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        'refresh_seconds = 300\n[[accounts]]\nslug = "one"\nname = "Same"\n'
        '[[accounts]]\nslug = "two"\nname = " Same "\n',
        encoding="utf-8",
    )

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "Duplicate account name: Same"


def test_load_config_rejects_unsafe_slug(tmp_path: Path) -> None:
    """Verify path traversal cannot be used as a slug"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        'refresh_seconds = 300\n[[accounts]]\nslug = "../personal"\nname = "Personal"\n',
        encoding="utf-8",
    )

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "Invalid account slug: ../personal"


@pytest.mark.parametrize("refresh_value", ["true", "0", "-1", "1.5", '"300"'])
def test_load_config_rejects_invalid_refresh_values(tmp_path: Path, refresh_value: str) -> None:
    """Verify refresh delay must be a positive non-boolean integer"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        f'refresh_seconds = {refresh_value}\n[[accounts]]\nslug = "one"\nname = "One"\n',
        encoding="utf-8",
    )

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "refresh_seconds must be a positive integer"


def test_load_config_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    """Verify top-level typos are not ignored"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        'refresh_seconds = 300\nunknown = 1\n[[accounts]]\nslug = "one"\nname = "One"\n',
        encoding="utf-8",
    )

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "Configuration has missing or unknown fields"


def test_load_config_rejects_unknown_account_field(tmp_path: Path) -> None:
    """Verify account-level typos are not ignored"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        'refresh_seconds = 300\n[[accounts]]\nslug = "one"\nname = "One"\nemail = "x"\n',
        encoding="utf-8",
    )

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "Account has missing or unknown fields"


def test_load_config_rejects_missing_account_field(tmp_path: Path) -> None:
    """Verify required account fields cannot be omitted"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text('refresh_seconds = 300\n[[accounts]]\nslug = "one"\n', encoding="utf-8")

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "Account has missing or unknown fields"


def test_load_config_rejects_non_table_account(tmp_path: Path) -> None:
    """Verify every account entry must be a TOML table"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text('refresh_seconds = 300\naccounts = ["one"]\n', encoding="utf-8")

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "Every account must be a TOML table"


def test_load_config_rejects_empty_name(tmp_path: Path) -> None:
    """Verify whitespace-only display names are rejected"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        'refresh_seconds = 300\n[[accounts]]\nslug = "one"\nname = "   "\n',
        encoding="utf-8",
    )

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == "Account name must not be empty"


@pytest.mark.parametrize("slug", ["Upper", "-leading", "a" * 65])
def test_load_config_rejects_invalid_slug_forms(tmp_path: Path, slug: str) -> None:
    """Verify unsafe slug forms are rejected"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text(
        f'refresh_seconds = 300\n[[accounts]]\nslug = "{slug}"\nname = "One"\n',
        encoding="utf-8",
    )

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == f"Invalid account slug: {slug}"


def test_load_config_chains_missing_file_error(tmp_path: Path) -> None:
    """Verify missing files retain their original cause"""

    # ARRANGE
    config_path: Path = tmp_path / "missing.toml"

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert str(caught.value) == f"Cannot read configuration: {config_path}"
    assert isinstance(caught.value.__cause__, OSError)


def test_load_config_chains_invalid_utf8_error(tmp_path: Path) -> None:
    """Verify invalid UTF-8 retains its original cause"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_bytes(b"\xff")

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


def test_load_config_chains_invalid_toml_error(tmp_path: Path) -> None:
    """Verify invalid TOML retains its original cause"""

    # ARRANGE
    config_path: Path = tmp_path / "config.toml"
    config_path.write_text("refresh_seconds = [", encoding="utf-8")

    # ACT
    with pytest.raises(ConfigError) as caught:
        load_config(config_path)

    # ASSERT
    assert isinstance(caught.value.__cause__, limits.tomllib.TOMLDecodeError)


def test_prepare_account_home_creates_exact_protected_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify isolated storage layout, contents, and permissions"""

    # ARRANGE
    state_root, accounts_root = set_state_roots(monkeypatch, tmp_path)
    account: AccountConfig = make_account(state_root)
    config_path: Path = account.codex_home / limits.CODEX_CONFIG_FILE_NAME
    expected_entries: Set[Path] = {
        Path("accounts"),
        Path("accounts/personal"),
        Path("accounts/personal/config.toml"),
    }

    # ACT
    prepare_account_home(account)
    actual_entries: Set[Path] = {path.relative_to(state_root) for path in state_root.rglob("*")}

    # ASSERT
    assert actual_entries == expected_entries
    assert config_path.read_text(encoding="utf-8") == limits.CODEX_CONFIG_CONTENT
    if limits.sys.platform != limits.WINDOWS_PLATFORM:
        assert stat.S_IMODE(state_root.stat().st_mode) == limits.DIRECTORY_MODE
        assert stat.S_IMODE(accounts_root.stat().st_mode) == limits.DIRECTORY_MODE
        assert stat.S_IMODE(account.codex_home.stat().st_mode) == limits.DIRECTORY_MODE
        assert stat.S_IMODE(config_path.stat().st_mode) == limits.FILE_MODE


def test_prepare_account_home_preserves_existing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify credential contents are untouched while permissions are fixed"""

    # ARRANGE
    state_root, _ = set_state_roots(monkeypatch, tmp_path)
    account: AccountConfig = make_account(state_root)
    account.codex_home.mkdir(parents=True)
    auth_path: Path = account.codex_home / limits.AUTH_FILE_NAME
    credential_bytes: bytes = b"opaque-credential-bytes"
    auth_path.write_bytes(credential_bytes)
    os.chmod(auth_path, 0o644)

    # ACT
    prepare_account_home(account)

    # ASSERT
    assert auth_path.read_bytes() == credential_bytes
    if limits.sys.platform != limits.WINDOWS_PLATFORM:
        assert stat.S_IMODE(auth_path.stat().st_mode) == limits.FILE_MODE


def test_prepare_account_home_chains_storage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify filesystem failures become safe login errors"""

    # ARRANGE
    state_root, _ = set_state_roots(monkeypatch, tmp_path)
    account: AccountConfig = make_account(state_root)

    def fail_mkdir(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    # ACT
    with pytest.raises(LoginError) as caught:
        prepare_account_home(account)

    # ASSERT
    assert str(caught.value) == "Cannot prepare account storage: personal"
    assert isinstance(caught.value.__cause__, OSError)


def test_build_codex_environment_copies_and_replaces_only_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify subprocess environments are isolated copies"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    monkeypatch.setenv("LIMITS_TEST_VALUE", "kept")
    original_home: Optional[str] = os.environ.get(limits.CODEX_HOME_ENV_NAME)

    # ACT
    environment: Dict[str, str] = build_codex_environment(account)

    # ASSERT
    assert environment is not os.environ
    assert environment["LIMITS_TEST_VALUE"] == "kept"
    assert environment[limits.CODEX_HOME_ENV_NAME] == str(account.codex_home)
    assert os.environ.get(limits.CODEX_HOME_ENV_NAME) == original_home


def test_login_account_uses_exact_command_and_inherited_stdio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify successful official login invocation and credential protection"""

    # ARRANGE
    state_root, _ = set_state_roots(monkeypatch, tmp_path)
    account: AccountConfig = make_account(state_root)
    captured_args: List[Tuple[object, ...]] = []
    captured_kwargs: List[Dict[str, object]] = []

    def create_auth() -> None:
        auth_path: Path = account.codex_home / limits.AUTH_FILE_NAME
        auth_path.write_bytes(b"opaque")
        os.chmod(auth_path, 0o644)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        captured_args.append(args)
        captured_kwargs.append(kwargs)
        return FakeProcess(wait_code=0, on_wait=create_auth)

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    asyncio.run(login_account(account))

    # ASSERT
    assert captured_args == [(limits.CODEX_COMMAND, limits.LOGIN_COMMAND)]
    assert len(captured_kwargs) == 1
    assert set(captured_kwargs[0]) == {"env"}
    environment: Dict[str, str] = captured_kwargs[0]["env"]
    assert environment[limits.CODEX_HOME_ENV_NAME] == str(account.codex_home)
    auth_path: Path = account.codex_home / limits.AUTH_FILE_NAME
    if limits.sys.platform != limits.WINDOWS_PLATFORM:
        assert stat.S_IMODE(auth_path.stat().st_mode) == limits.FILE_MODE


def test_login_account_rejects_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify nonzero Codex login exits are reported safely"""

    # ARRANGE
    state_root, _ = set_state_roots(monkeypatch, tmp_path)
    account: AccountConfig = make_account(state_root)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess(wait_code=7)

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(LoginError) as caught:
        asyncio.run(login_account(account))

    # ASSERT
    assert str(caught.value) == "Codex login failed for personal"


def test_login_account_requires_regular_auth_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify successful CLI exit must create credentials"""

    # ARRANGE
    state_root, _ = set_state_roots(monkeypatch, tmp_path)
    account: AccountConfig = make_account(state_root)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess(wait_code=0)

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(LoginError) as caught:
        asyncio.run(login_account(account))

    # ASSERT
    assert str(caught.value) == "Codex did not create credentials for personal"


def test_login_account_chains_spawn_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify login spawn failures retain their cause"""

    # ARRANGE
    state_root, _ = set_state_roots(monkeypatch, tmp_path)
    account: AccountConfig = make_account(state_root)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(LoginError) as caught:
        asyncio.run(login_account(account))

    # ASSERT
    assert str(caught.value) == "Cannot start Codex login: personal"
    assert isinstance(caught.value.__cause__, OSError)


def test_login_account_propagates_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify login cancellation is never converted"""

    # ARRANGE
    state_root, _ = set_state_roots(monkeypatch, tmp_path)
    account: AccountConfig = make_account(state_root)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        raise asyncio.CancelledError()

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(login_account(account))

    # ASSERT
    assert not (account.codex_home / limits.AUTH_FILE_NAME).exists()


def test_rpc_builders_match_wire_contract() -> None:
    """Verify all fixed App Server messages as whole mappings"""

    # ARRANGE
    expected: List[Dict[str, object]] = [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "chatgpt-limits",
                    "title": "ChatGPT Limits",
                    "version": "1.0.0",
                },
            },
        },
        {"method": "initialized", "params": {}},
        {"method": "account/rateLimits/read", "id": 2},
    ]

    # ACT
    actual: List[Dict[str, object]] = [
        build_initialize_request(),
        build_initialized_notification(),
        build_rate_limits_request(),
    ]

    # ASSERT
    assert actual == expected
    assert all("jsonrpc" not in message for message in actual)
    assert "capabilities" not in str(actual)
    assert "protocolVersion" not in str(actual)


def test_write_rpc_message_writes_compact_utf8_and_drains() -> None:
    """Verify one compact JSONL message is flushed"""

    # ARRANGE
    stdin: FakeStdin = FakeStdin()
    message: Dict[str, object] = {"method": "méthod", "id": 2}
    expected: bytes = b'{"method":"m\xc3\xa9thod","id":2}\n'

    # ACT
    asyncio.run(write_rpc_message(stdin, message))

    # ASSERT
    assert b"".join(stdin.chunks) == expected
    assert stdin.drain_calls == 1


def test_read_rpc_result_ignores_notifications_and_foreign_ids() -> None:
    """Verify the reader waits for the requested response identifier"""

    # ARRANGE
    stdout: FakeStdout = FakeStdout(
        [
            rpc_line({"method": "notice", "params": {}}),
            rpc_line({"id": 99, "result": {"foreign": True}}),
            rpc_line({"id": 2, "result": {"rateLimits": {}}}),
        ],
    )
    expected: Dict[str, object] = {"rateLimits": {}}

    # ACT
    actual: Dict[str, object] = asyncio.run(read_rpc_result(stdout, 2))

    # ASSERT
    assert actual == expected
    assert stdout.read_count == 3


def test_read_rpc_result_rejects_matching_rpc_error() -> None:
    """Verify matching RPC errors become safe App Server errors"""

    # ARRANGE
    stdout: FakeStdout = FakeStdout([rpc_line({"id": 2, "error": {"code": -1}})])

    # ACT
    with pytest.raises(AppServerError) as caught:
        asyncio.run(read_rpc_result(stdout, 2))

    # ASSERT
    assert str(caught.value) == "Codex App Server rejected request 2"


def test_read_rpc_result_rejects_eof() -> None:
    """Verify premature EOF becomes a protocol error"""

    # ARRANGE
    stdout: FakeStdout = FakeStdout([])

    # ACT
    with pytest.raises(ProtocolError) as caught:
        asyncio.run(read_rpc_result(stdout, 2))

    # ASSERT
    assert str(caught.value) == "Codex App Server closed stdout"


def test_read_rpc_result_rejects_non_object_message() -> None:
    """Verify JSON scalars cannot masquerade as responses"""

    # ARRANGE
    stdout: FakeStdout = FakeStdout([b"[]\n"])

    # ACT
    with pytest.raises(ProtocolError) as caught:
        asyncio.run(read_rpc_result(stdout, 2))

    # ASSERT
    assert str(caught.value) == "Codex App Server returned a non-object message"


@pytest.mark.parametrize("result_value", [None, [], "invalid", 3])
def test_read_rpc_result_requires_result_object(result_value: object) -> None:
    """Verify the matching result must be an object"""

    # ARRANGE
    stdout: FakeStdout = FakeStdout([rpc_line({"id": 2, "result": result_value})])

    # ACT
    with pytest.raises(ProtocolError) as caught:
        asyncio.run(read_rpc_result(stdout, 2))

    # ASSERT
    assert str(caught.value) == "Codex App Server response has no result object"


@pytest.mark.parametrize("line", [b"\xff\n", b"{invalid}\n"])
def test_read_rpc_result_chains_invalid_json_errors(line: bytes) -> None:
    """Verify invalid UTF-8 and JSON retain decoding causes"""

    # ARRANGE
    stdout: FakeStdout = FakeStdout([line])

    # ACT
    with pytest.raises(ProtocolError) as caught:
        asyncio.run(read_rpc_result(stdout, 2))

    # ASSERT
    assert str(caught.value) == "Codex App Server returned invalid JSON"
    assert isinstance(caught.value.__cause__, (UnicodeDecodeError, json.JSONDecodeError))


def test_read_rpc_result_chains_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify request timeout is deterministic and preserves its cause"""

    # ARRANGE
    stdout: NeverStdout = NeverStdout()
    monkeypatch.setattr(limits, "REQUEST_TIMEOUT_SECONDS", 0.01)

    # ACT
    with pytest.raises(AppServerError) as caught:
        asyncio.run(read_rpc_result(stdout, 2))

    # ASSERT
    assert str(caught.value) == "Codex App Server request 2 timed out"
    assert isinstance(caught.value.__cause__, TimeoutError)


def test_parse_official_rate_limit_fixture() -> None:
    """Verify the documented Codex rate-limit response shape"""

    # ARRANGE
    result: Dict[str, object] = {
        "rateLimits": {
            "limitId": "codex",
            "limitName": None,
            "primary": {
                "usedPercent": 25,
                "windowDurationMins": 15,
                "resetsAt": 1730947200,
            },
            "secondary": None,
            "rateLimitReachedType": None,
        },
    }
    reset_time: datetime = datetime.fromtimestamp(
        1730947200,
        tz=timezone.utc,
    ).astimezone()
    expected: Dict[int, LimitWindow] = {
        15: LimitWindow(
            duration_minutes=15,
            used_percent=25.0,
            resets_at=reset_time,
        ),
    }

    # ACT
    actual: Dict[int, LimitWindow] = parse_limit_windows(result)

    # ASSERT
    assert actual == expected


def test_parse_limit_windows_returns_only_primary_five_hour() -> None:
    """Verify a single primary product window is accepted"""

    # ARRANGE
    result: Dict[str, object] = {
        "rateLimits": {
            "primary": {"usedPercent": 25, "windowDurationMins": 300},
            "secondary": None,
        },
    }
    expected: Dict[int, LimitWindow] = {
        300: LimitWindow(300, 25.0, None),
    }

    # ACT
    actual: Dict[int, LimitWindow] = parse_limit_windows(result)

    # ASSERT
    assert actual == expected


def test_parse_limit_windows_accepts_secondary_when_primary_is_null() -> None:
    """Verify position does not determine the weekly window"""

    # ARRANGE
    result: Dict[str, object] = {
        "rateLimits": {
            "primary": None,
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
        },
    }
    expected: Dict[int, LimitWindow] = {
        10080: LimitWindow(10080, 40.0, None),
    }

    # ACT
    actual: Dict[int, LimitWindow] = parse_limit_windows(result)

    # ASSERT
    assert actual == expected


def test_parse_limit_windows_returns_empty_mapping_for_null_windows() -> None:
    """Verify absent windows are data absence rather than errors"""

    # ARRANGE
    result: Dict[str, object] = {"rateLimits": {"primary": None, "secondary": None}}
    expected: Dict[int, LimitWindow] = {}

    # ACT
    actual: Dict[int, LimitWindow] = parse_limit_windows(result)

    # ASSERT
    assert actual == expected


@pytest.mark.parametrize("reverse", [False, True])
def test_parse_limit_windows_identifies_product_windows_by_duration(reverse: bool) -> None:
    """Verify product windows are independent of primary and secondary order"""

    # ARRANGE
    five_hour: Dict[str, object] = {"usedPercent": 25, "windowDurationMins": 300}
    week: Dict[str, object] = {"usedPercent": 57.5, "windowDurationMins": 10080}
    primary: Dict[str, object] = week if reverse else five_hour
    secondary: Dict[str, object] = five_hour if reverse else week
    result: Dict[str, object] = {"rateLimits": {"primary": primary, "secondary": secondary}}
    expected_durations: Set[int] = {
        limits.FIVE_HOUR_MINUTES,
        limits.WEEK_MINUTES,
    }

    # ACT
    windows: Dict[int, LimitWindow] = parse_limit_windows(result)
    actual_durations: Set[int] = set(windows)

    # ASSERT
    assert actual_durations == expected_durations


def test_select_rate_limit_bucket_prefers_codex_bucket() -> None:
    """Verify the named Codex bucket wins over the fallback"""

    # ARRANGE
    codex_bucket: Dict[str, object] = {"primary": None, "source": "named"}
    fallback_bucket: Dict[str, object] = {"primary": None, "source": "fallback"}
    result: Dict[str, object] = {
        "rateLimitsByLimitId": {"codex": codex_bucket},
        "rateLimits": fallback_bucket,
    }

    # ACT
    actual: Dict[str, object] = limits.select_rate_limit_bucket(result)

    # ASSERT
    assert actual == codex_bucket


def test_select_rate_limit_bucket_falls_back_to_rate_limits() -> None:
    """Verify the canonical fallback bucket remains supported"""

    # ARRANGE
    fallback_bucket: Dict[str, object] = {"primary": None}
    result: Dict[str, object] = {
        "rateLimitsByLimitId": {"codex_other": {"primary": {}}},
        "rateLimits": fallback_bucket,
    }

    # ACT
    actual: Dict[str, object] = limits.select_rate_limit_bucket(result)

    # ASSERT
    assert actual == fallback_bucket


def test_select_rate_limit_bucket_never_chooses_arbitrary_bucket() -> None:
    """Verify similarly named or first buckets are ignored"""

    # ARRANGE
    result: Dict[str, object] = {
        "rateLimitsByLimitId": {"codex_other": {"primary": None}},
    }

    # ACT
    with pytest.raises(ProtocolError) as caught:
        limits.select_rate_limit_bucket(result)

    # ASSERT
    assert str(caught.value) == "Codex rate-limit bucket is missing"


def test_parse_limit_window_accepts_null() -> None:
    """Verify null windows remain absent"""

    # ARRANGE
    value: object = None

    # ACT
    actual: Optional[LimitWindow] = parse_limit_window(value, "primary")

    # ASSERT
    assert actual is None


def test_parse_limit_window_accepts_missing_reset() -> None:
    """Verify reset time is optional"""

    # ARRANGE
    value: Dict[str, object] = {"usedPercent": "25", "windowDurationMins": "300"}
    expected: LimitWindow = LimitWindow(300, 25.0, None)

    # ACT
    actual: Optional[LimitWindow] = parse_limit_window(value, "primary")

    # ASSERT
    assert actual == expected


@pytest.mark.parametrize("timestamp", [1730947200, 1730947200.0])
def test_parse_limit_window_converts_unix_timestamp(timestamp: object) -> None:
    """Verify integer and float Unix seconds become aware local datetimes"""

    # ARRANGE
    value: Dict[str, object] = {
        "usedPercent": 25,
        "windowDurationMins": 300,
        "resetsAt": timestamp,
    }
    expected_reset: datetime = datetime.fromtimestamp(1730947200, tz=timezone.utc).astimezone()
    expected: LimitWindow = LimitWindow(300, 25.0, expected_reset)

    # ACT
    actual: Optional[LimitWindow] = parse_limit_window(value, "primary")

    # ASSERT
    assert actual == expected
    assert actual is not None
    assert actual.resets_at is not None
    assert actual.resets_at.tzinfo is not None


def test_parse_limit_windows_rejects_duplicate_duration() -> None:
    """Verify a second window cannot silently overwrite the first"""

    # ARRANGE
    result: Dict[str, object] = {
        "rateLimits": {
            "primary": {"usedPercent": 10, "windowDurationMins": 300},
            "secondary": {"usedPercent": 20, "windowDurationMins": 300},
        },
    }

    # ACT
    with pytest.raises(ProtocolError) as caught:
        parse_limit_windows(result)

    # ASSERT
    assert str(caught.value) == "Codex returned duplicate limit durations"


@pytest.mark.parametrize("used_value", [True, None, "bad", [], {}])
def test_parse_limit_window_rejects_nonnumeric_used_percent(used_value: object) -> None:
    """Verify used percentage must be numeric"""

    # ARRANGE
    value: Dict[str, object] = {"usedPercent": used_value, "windowDurationMins": 300}

    # ACT
    with pytest.raises(ProtocolError) as caught:
        parse_limit_window(value, "primary")

    # ASSERT
    assert str(caught.value) == "primary usedPercent must be a number"


@pytest.mark.parametrize("used_value", [float("nan"), float("inf"), -0.1, 100.1])
def test_parse_limit_window_rejects_used_percent_outside_range(used_value: object) -> None:
    """Verify used percentage is finite and within zero to one hundred"""

    # ARRANGE
    value: Dict[str, object] = {"usedPercent": used_value, "windowDurationMins": 300}

    # ACT
    with pytest.raises(ProtocolError) as caught:
        parse_limit_window(value, "primary")

    # ASSERT
    assert str(caught.value) == "primary usedPercent is outside 0..100"


@pytest.mark.parametrize(
    "duration_value",
    [True, None, "bad", float("nan"), float("inf"), 0, -1, 300.5],
)
def test_parse_limit_window_rejects_invalid_duration(duration_value: object) -> None:
    """Verify duration must be finite, positive, and integral"""

    # ARRANGE
    value: Dict[str, object] = {"usedPercent": 25, "windowDurationMins": duration_value}

    # ACT
    with pytest.raises(ProtocolError) as caught:
        parse_limit_window(value, "primary")

    # ASSERT
    assert str(caught.value) == "primary windowDurationMins must be a positive integer"


@pytest.mark.parametrize("reset_value", [True, "bad", [], float("nan"), float("inf")])
def test_parse_limit_window_rejects_invalid_reset_type_or_number(reset_value: object) -> None:
    """Verify reset must be finite Unix seconds"""

    # ARRANGE
    value: Dict[str, object] = {
        "usedPercent": 25,
        "windowDurationMins": 300,
        "resetsAt": reset_value,
    }

    # ACT
    with pytest.raises(ProtocolError) as caught:
        parse_limit_window(value, "primary")

    # ASSERT
    assert str(caught.value) == "primary resetsAt must be a Unix timestamp"


@pytest.mark.parametrize("reset_value", [1e30, 10 ** 500])
def test_parse_limit_window_chains_out_of_range_reset(reset_value: object) -> None:
    """Verify unsupported timestamp ranges become chained protocol errors"""

    # ARRANGE
    value: Dict[str, object] = {
        "usedPercent": 25,
        "windowDurationMins": 300,
        "resetsAt": reset_value,
    }

    # ACT
    with pytest.raises(ProtocolError) as caught:
        parse_limit_window(value, "primary")

    # ASSERT
    assert str(caught.value) == "primary resetsAt is outside the supported range"
    assert isinstance(caught.value.__cause__, (OSError, OverflowError, ValueError))


def test_parse_limit_window_rejects_non_object_shape() -> None:
    """Verify a present window must be an object"""

    # ARRANGE
    value: object = "invalid"

    # ACT
    with pytest.raises(ProtocolError) as caught:
        parse_limit_window(value, "secondary")

    # ASSERT
    assert str(caught.value) == "secondary limit window must be an object"


def test_login_account_chains_credential_protection_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify credential chmod failures retain their cause"""

    # ARRANGE
    state_root, _ = set_state_roots(monkeypatch, tmp_path)
    account: AccountConfig = make_account(state_root)
    auth_path: Path = account.codex_home / limits.AUTH_FILE_NAME
    original_chmod: Callable[[Path, int], None] = limits.os.chmod

    def create_auth() -> None:
        auth_path.write_bytes(b"opaque")

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess(wait_code=0, on_wait=create_auth)

    def fail_auth_chmod(path: Path, mode: int) -> None:
        if Path(path) == auth_path:
            raise OSError("synthetic chmod failure")
        original_chmod(path, mode)

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(limits.os, "chmod", fail_auth_chmod)

    # ACT
    with pytest.raises(LoginError) as caught:
        asyncio.run(login_account(account))

    # ASSERT
    assert str(caught.value) == "Cannot protect credentials for personal"
    assert isinstance(caught.value.__cause__, OSError)


def test_read_account_limits_checks_auth_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify missing credentials fail before creating a subprocess"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    spawn_calls: List[Tuple[object, ...]] = []

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        spawn_calls.append(args)
        return FakeProcess()

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(AppServerError) as caught:
        asyncio.run(read_account_limits(account))

    # ASSERT
    expected: str = (
        "Account is not logged in; run --login personal "
        "with the same --config path"
    )
    assert str(caught.value) == expected
    assert spawn_calls == []


def test_read_account_limits_uses_exact_process_contract_and_handshake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify App Server argv, streams, environment, sequence, and cleanup"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    account.codex_home.mkdir(parents=True)
    (account.codex_home / limits.AUTH_FILE_NAME).write_bytes(b"opaque")
    stdin: FakeStdin = FakeStdin()
    expected_initialize: Dict[str, object] = build_initialize_request()

    def check_handshake_gate(read_index: int) -> None:
        if read_index == 0:
            assert written_messages(stdin) == [expected_initialize]

    expected_result: Dict[str, object] = {"rateLimits": {"primary": None, "secondary": None}}
    stdout: FakeStdout = FakeStdout(
        [
            rpc_line({"id": limits.INITIALIZE_REQUEST_ID, "result": {"ready": True}}),
            rpc_line({"id": limits.RATE_LIMITS_REQUEST_ID, "result": expected_result}),
        ],
        on_read=check_handshake_gate,
    )
    process: FakeProcess = FakeProcess(stdin=stdin, stdout=stdout)
    captured_args: List[Tuple[object, ...]] = []
    captured_kwargs: List[Dict[str, object]] = []
    stderr_objects: List[BinaryIO] = []

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        captured_args.append(args)
        captured_kwargs.append(kwargs)
        stderr_objects.append(kwargs["stderr"])
        return process

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    actual: Dict[str, object] = asyncio.run(read_account_limits(account))

    # ASSERT
    assert actual == expected_result
    assert captured_args == [(limits.CODEX_COMMAND, limits.APP_SERVER_COMMAND)]
    assert len(captured_kwargs) == 1
    process_options: Dict[str, object] = captured_kwargs[0]
    assert set(process_options) == {"stdin", "stdout", "stderr", "env"}
    assert process_options["stdin"] == asyncio.subprocess.PIPE
    assert process_options["stdout"] == asyncio.subprocess.PIPE
    assert process_options["stderr"] not in (asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL)
    environment: Dict[str, str] = process_options["env"]
    assert environment[limits.CODEX_HOME_ENV_NAME] == str(account.codex_home)
    assert written_messages(stdin) == [
        build_initialize_request(),
        build_initialized_notification(),
        build_rate_limits_request(),
    ]
    assert stdin.drain_calls == 3
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert stderr_objects[0].closed


def test_read_account_limits_chains_spawn_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify App Server spawn failures retain their cause"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    account.codex_home.mkdir(parents=True)
    (account.codex_home / limits.AUTH_FILE_NAME).write_bytes(b"opaque")

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(AppServerError) as caught:
        asyncio.run(read_account_limits(account))

    # ASSERT
    assert str(caught.value) == "Cannot start Codex App Server: personal"
    assert isinstance(caught.value.__cause__, OSError)


def test_read_account_limits_stops_after_initialize_rpc_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a failed handshake blocks all later requests and stops the process"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    account.codex_home.mkdir(parents=True)
    (account.codex_home / limits.AUTH_FILE_NAME).write_bytes(b"opaque")
    stdin: FakeStdin = FakeStdin()
    stdout: FakeStdout = FakeStdout(
        [rpc_line({"id": limits.INITIALIZE_REQUEST_ID, "error": {"code": -1}})],
    )
    process: FakeProcess = FakeProcess(stdin=stdin, stdout=stdout)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(AppServerError) as caught:
        asyncio.run(read_account_limits(account))

    # ASSERT
    assert str(caught.value) == "Codex App Server rejected request 1"
    assert written_messages(stdin) == [build_initialize_request()]
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_read_account_limits_waits_for_exact_rate_limit_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify foreign messages after handshake are ignored"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    account.codex_home.mkdir(parents=True)
    (account.codex_home / limits.AUTH_FILE_NAME).write_bytes(b"opaque")
    stdin: FakeStdin = FakeStdin()
    expected: Dict[str, object] = {"rateLimits": {"primary": None}}
    stdout: FakeStdout = FakeStdout(
        [
            rpc_line({"id": 1, "result": {}}),
            rpc_line({"method": "account/updated", "params": {}}),
            rpc_line({"id": 77, "result": {"foreign": True}}),
            rpc_line({"id": 2, "result": expected}),
        ],
    )
    process: FakeProcess = FakeProcess(stdin=stdin, stdout=stdout)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    actual: Dict[str, object] = asyncio.run(read_account_limits(account))

    # ASSERT
    assert actual == expected
    assert stdout.read_count == 4
    assert written_messages(stdin) == [
        build_initialize_request(),
        build_initialized_notification(),
        build_rate_limits_request(),
    ]


@pytest.mark.parametrize("missing_stream", ["stdin", "stdout"])
def test_read_account_limits_rejects_missing_process_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_stream: str,
) -> None:
    """Verify unavailable subprocess streams become safe errors"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    account.codex_home.mkdir(parents=True)
    (account.codex_home / limits.AUTH_FILE_NAME).write_bytes(b"opaque")
    stdin: Optional[FakeStdin] = None if missing_stream == "stdin" else FakeStdin()
    stdout: Optional[FakeStdout] = None if missing_stream == "stdout" else FakeStdout([])
    process: FakeProcess = FakeProcess(stdin=stdin, stdout=stdout)
    expected: str = f"Codex App Server {missing_stream} is unavailable"

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(AppServerError) as caught:
        asyncio.run(read_account_limits(account))

    # ASSERT
    assert str(caught.value) == expected
    assert process.terminate_calls == 1


def test_read_account_limits_chains_transport_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify broken process transport becomes an account-specific error"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    account.codex_home.mkdir(parents=True)
    (account.codex_home / limits.AUTH_FILE_NAME).write_bytes(b"opaque")
    stdin: BrokenStdin = BrokenStdin()
    stdout: FakeStdout = FakeStdout([])
    process: FakeProcess = FakeProcess(stdin=stdin, stdout=stdout)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(AppServerError) as caught:
        asyncio.run(read_account_limits(account))

    # ASSERT
    assert str(caught.value) == "Codex App Server transport failed: personal"
    assert isinstance(caught.value.__cause__, OSError)
    assert process.terminate_calls == 1


def test_stop_process_terminates_without_kill() -> None:
    """Verify normal child shutdown uses terminate only"""

    # ARRANGE
    process: FakeProcess = FakeProcess()

    # ACT
    asyncio.run(stop_process(process))

    # ASSERT
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls == 1
    assert process.returncode == 0


def test_stop_process_skips_finished_child() -> None:
    """Verify already-finished children are not signaled"""

    # ARRANGE
    process: FakeProcess = FakeProcess()
    process.returncode = 4

    # ACT
    asyncio.run(stop_process(process))

    # ASSERT
    assert process.terminate_calls == 0
    assert process.kill_calls == 0
    assert process.wait_calls == 0


def test_stop_process_kills_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify shutdown timeout falls back to kill and a final wait"""

    # ARRANGE
    process: FakeProcess = FakeProcess(never_exit=True)
    monkeypatch.setattr(limits, "SHUTDOWN_TIMEOUT_SECONDS", 0.01)

    # ACT
    asyncio.run(stop_process(process))

    # ASSERT
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2
    assert process.returncode == 0


def test_stop_process_kills_and_reaps_when_shutdown_is_cancelled() -> None:
    """Verify cancellation interrupts waiting without leaving the child active"""

    # ARRANGE
    process: FakeProcess = FakeProcess(never_exit=True)

    async def cancel_shutdown() -> None:
        task: asyncio.Task[None] = asyncio.create_task(stop_process(process))
        while process.wait_calls == 0:
            await asyncio.sleep(0)
        task.cancel()
        await task

    # ACT
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_shutdown())

    # ASSERT
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2
    assert process.returncode == 0


def test_read_account_limits_propagates_cancellation_and_stops_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify cancellation leaves no active App Server child"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    account.codex_home.mkdir(parents=True)
    (account.codex_home / limits.AUTH_FILE_NAME).write_bytes(b"opaque")
    stdin: FakeStdin = FakeStdin()
    stdout: NeverStdout = NeverStdout()
    process: FakeProcess = FakeProcess(stdin=stdin, stdout=stdout)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return process

    async def cancel_active_read() -> None:
        task: asyncio.Task[Dict[str, object]] = asyncio.create_task(read_account_limits(account))
        await stdout.started.wait()
        task.cancel()
        await task

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_active_read())

    # ASSERT
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.returncode == 0


def test_read_account_limits_adds_safe_stderr_note_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify bounded diagnostics are exception notes rather than display text"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    account.codex_home.mkdir(parents=True)
    (account.codex_home / limits.AUTH_FILE_NAME).write_bytes(b"opaque")
    stdin: FakeStdin = FakeStdin()
    stdout: FakeStdout = FakeStdout([b"not-json\n"])
    process: FakeProcess = FakeProcess(stdin=stdin, stdout=stdout)

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        stderr_file: BinaryIO = kwargs["stderr"]
        stderr_file.write(b"safe synthetic diagnostic")
        return process

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)

    # ACT
    with pytest.raises(ProtocolError) as caught:
        asyncio.run(read_account_limits(account))

    # ASSERT
    expected_notes: List[str] = [
        f"{limits.STDERR_NOTE_PREFIX}: safe synthetic diagnostic",
    ]
    assert caught.value.__notes__ == expected_notes
    assert "safe synthetic diagnostic" not in str(caught.value)
    assert process.terminate_calls == 1


@pytest.mark.parametrize("marker", limits.SENSITIVE_STDERR_MARKERS)
def test_read_process_stderr_redacts_each_sensitive_marker(marker: str) -> None:
    """Verify any case-insensitive sensitive marker redacts the whole tail"""

    # ARRANGE
    stderr_file: BinaryIO = tempfile.TemporaryFile(mode="w+b")
    with stderr_file:
        stderr_file.write(f"diagnostic {marker.upper()} value".encode("utf-8"))

        # ACT
        actual: Optional[str] = read_process_stderr(stderr_file)

        # ASSERT
        assert actual == limits.REDACTED_STDERR_TEXT


def test_read_process_stderr_returns_none_for_empty_file() -> None:
    """Verify an empty diagnostic file produces no note"""

    # ARRANGE
    stderr_file: BinaryIO = tempfile.TemporaryFile(mode="w+b")
    with stderr_file:

        # ACT
        actual: Optional[str] = read_process_stderr(stderr_file)

        # ASSERT
        assert actual is None


def test_read_process_stderr_reads_only_bounded_tail() -> None:
    """Verify diagnostic data is capped at the configured byte bound"""

    # ARRANGE
    stderr_file: BinaryIO = tempfile.TemporaryFile(mode="w+b")
    with stderr_file:
        stderr_file.write(b"x" * (limits.MAX_STDERR_BYTES * 2))
        expected: str = "x" * limits.MAX_STDERR_BYTES

        # ACT
        actual: Optional[str] = read_process_stderr(stderr_file)

        # ASSERT
        assert actual == expected
        assert actual is not None
        assert len(actual.encode("utf-8")) <= limits.MAX_STDERR_BYTES


def test_read_process_stderr_replaces_invalid_utf8() -> None:
    """Verify malformed diagnostic bytes cannot crash sanitization"""

    # ARRANGE
    stderr_file: BinaryIO = tempfile.TemporaryFile(mode="w+b")
    with stderr_file:
        stderr_file.write(b"safe \xff diagnostic")

        # ACT
        actual: Optional[str] = read_process_stderr(stderr_file)

        # ASSERT
        assert actual == "safe \ufffd diagnostic"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(75.0, "75"), (42.5, "42.5"), (42.54, "42.5"), (42.56, "42.6")],
)
def test_format_percentage_uses_at_most_one_decimal(value: float, expected: str) -> None:
    """Verify compact percentage formatting"""

    # ARRANGE
    percentage: float = value

    # ACT
    actual: str = limits.format_percentage(percentage)

    # ASSERT
    assert actual == expected


@pytest.mark.parametrize(("seconds", "expected"), [(300, "5 мин"), (60, "1 мин"), (90, "90 с")])
def test_format_refresh_interval_uses_whole_minutes_only(seconds: int, expected: str) -> None:
    """Verify configured intervals use the normative units"""

    # ARRANGE
    refresh_seconds: int = seconds

    # ACT
    actual: str = limits.format_refresh_interval(refresh_seconds)

    # ASSERT
    assert actual == expected


def test_format_limit_line_shows_remaining_and_unknown_reset() -> None:
    """Verify used percentage is converted to remaining only at render time"""

    # ARRANGE
    window: LimitWindow = LimitWindow(
        duration_minutes=limits.FIVE_HOUR_MINUTES,
        used_percent=25.0,
        resets_at=None,
    )
    expected: str = "  5 часов: [███████████████░░░░░] 75% осталось, сброс неизвестен"

    # ACT
    actual: str = limits.format_limit_line(limits.FIVE_HOUR_LABEL, window)

    # ASSERT
    assert actual == expected


def test_format_limit_line_shows_no_data_without_guessing() -> None:
    """Verify an absent window is never rendered as full remaining usage"""

    # ARRANGE
    window: Optional[LimitWindow] = None

    # ACT
    actual: str = limits.format_limit_line(limits.WEEK_LABEL, window)

    # ASSERT
    assert actual == "  Неделя: нет данных"
    assert "100%" not in actual


def test_format_limit_line_formats_reset_in_local_timezone() -> None:
    """Verify reset timestamps are rendered in the local timezone"""

    # ARRANGE
    reset_time: datetime = datetime(2026, 7, 21, 11, 20, tzinfo=timezone.utc)
    window: LimitWindow = LimitWindow(300, 25.0, reset_time)
    local_text: str = reset_time.astimezone().strftime(limits.DATETIME_FORMAT)
    expected: str = f"  5 часов: [███████████████░░░░░] 75% осталось, сброс {local_text}"

    # ACT
    actual: str = limits.format_limit_line(limits.FIVE_HOUR_LABEL, window)

    # ASSERT
    assert actual == expected


def test_render_screen_matches_full_two_account_snapshot() -> None:
    """Verify exact headings, spacing, account order, and footer"""

    # ARRANGE
    root: Path = Path("/tmp/chatgpt-limits")
    personal: AccountConfig = make_account(root, "personal", "Personal Pro")
    work: AccountConfig = make_account(root, "work", "Work Pro")
    first_reset: datetime = datetime(2026, 7, 21, 11, 20, tzinfo=timezone.utc)
    second_reset: datetime = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)
    third_reset: datetime = datetime(2026, 7, 21, 13, 10, tzinfo=timezone.utc)
    statuses: List[AccountStatus] = [
        AccountStatus(
            account=personal,
            windows={
                300: LimitWindow(300, 25.0, first_reset),
                10080: LimitWindow(10080, 57.5, second_reset),
            },
            error_message=None,
        ),
        AccountStatus(
            account=work,
            windows={300: LimitWindow(300, 0.0, third_reset)},
            error_message=None,
        ),
    ]
    refreshed_at: datetime = datetime(2026, 7, 21, 9, 5, tzinfo=timezone.utc)
    expected: str = "\n".join(
        [
            "ChatGPT Pro / Codex limits",
            f"Обновлено: {limits.format_datetime(refreshed_at)}",
            "Следующее обновление: примерно через 5 мин",
            "",
            "Personal Pro",
            f"  5 часов: [███████████████░░░░░] 75% осталось, сброс {limits.format_datetime(first_reset)}",
            f"  Неделя: [████████░░░░░░░░░░░░] 42.5% осталось, сброс {limits.format_datetime(second_reset)}",
            "",
            "Work Pro",
            f"  5 часов: [████████████████████] 100% осталось, сброс {limits.format_datetime(third_reset)}",
            "  Неделя: нет данных",
            "",
            "Остановка: Ctrl+C",
        ],
    )

    # ACT
    actual: str = render_screen(statuses, refreshed_at, 300)

    # ASSERT
    assert actual == expected
    assert not actual.endswith("\n")


def test_render_screen_ignores_unrecognized_durations() -> None:
    """Verify non-product windows are retained internally but not displayed"""

    # ARRANGE
    account: AccountConfig = make_account(Path("/tmp/chatgpt-limits"))
    status: AccountStatus = AccountStatus(
        account=account,
        windows={15: LimitWindow(15, 25.0, None)},
        error_message=None,
    )
    refreshed_at: datetime = datetime(2026, 7, 21, 9, 5, tzinfo=timezone.utc)

    # ACT
    screen: str = render_screen([status], refreshed_at, 300)

    # ASSERT
    assert "5 часов: нет данных" in screen
    assert "Неделя: нет данных" in screen
    assert "15" not in screen


def test_render_screen_shows_only_safe_account_error() -> None:
    """Verify exception notes and stale limit lines cannot reach the screen"""

    # ARRANGE
    account: AccountConfig = make_account(Path("/tmp/chatgpt-limits"))
    error: ProtocolError = ProtocolError("safe account failure")
    error.add_note("diagnostic note must stay in log")
    status: AccountStatus = AccountStatus(
        account=account,
        windows={300: LimitWindow(300, 25.0, None)},
        error_message=str(error),
    )
    refreshed_at: datetime = datetime(2026, 7, 21, 9, 5, tzinfo=timezone.utc)

    # ACT
    screen: str = render_screen([status], refreshed_at, 300)

    # ASSERT
    assert "  Ошибка: safe account failure" in screen
    assert "5 часов:" not in screen
    assert "Неделя:" not in screen
    assert "diagnostic note" not in screen
    assert "Traceback" not in screen


def test_render_screen_preserves_three_account_order() -> None:
    """Verify any configured account count follows status order"""

    # ARRANGE
    root: Path = Path("/tmp/chatgpt-limits")
    statuses: List[AccountStatus] = [
        AccountStatus(make_account(root, "one", "One"), {}, None),
        AccountStatus(make_account(root, "two", "Two"), {}, None),
        AccountStatus(make_account(root, "three", "Three"), {}, None),
    ]
    refreshed_at: datetime = datetime(2026, 7, 21, 9, 5, tzinfo=timezone.utc)

    # ACT
    screen: str = render_screen(statuses, refreshed_at, 90)

    # ASSERT
    assert screen.index("One") < screen.index("Two") < screen.index("Three")
    assert "Следующее обновление: примерно через 90 с" in screen


def test_write_screen_clears_tty_and_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify TTY snapshots replace the current screen"""

    # ARRANGE
    terminal: FakeTerminal = FakeTerminal(is_tty=True)
    monkeypatch.setattr(limits.sys, "stdout", terminal)

    # ACT
    limits.write_screen("snapshot")

    # ASSERT
    assert terminal.text() == f"{limits.CLEAR_SCREEN}snapshot\n"
    assert terminal.flushed


def test_write_screen_appends_non_tty_without_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify redirected snapshots remain plain text"""

    # ARRANGE
    terminal: FakeTerminal = FakeTerminal(is_tty=False)
    monkeypatch.setattr(limits.sys, "stdout", terminal)

    # ACT
    limits.write_screen("snapshot")

    # ASSERT
    assert terminal.text() == "\nsnapshot\n"
    assert limits.CLEAR_SCREEN not in terminal.text()
    assert terminal.flushed


def test_get_account_status_returns_parsed_success_without_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify successful account reads become complete display state"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    result: Dict[str, object] = {
        "rateLimits": {
            "primary": {"usedPercent": 25, "windowDurationMins": 300},
            "secondary": None,
        },
    }
    log_exception: Mock = Mock()

    async def fake_read(selected: AccountConfig) -> Dict[str, object]:
        assert selected == account
        return result

    monkeypatch.setattr(limits, "read_account_limits", fake_read)
    monkeypatch.setattr(limits.logger, "exception", log_exception)
    expected: AccountStatus = AccountStatus(
        account=account,
        windows={300: LimitWindow(300, 25.0, None)},
        error_message=None,
    )

    # ACT
    actual: AccountStatus = asyncio.run(limits.get_account_status(account))

    # ASSERT
    assert actual == expected
    log_exception.assert_not_called()


def test_get_account_status_logs_known_error_and_hides_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify known account failures are isolated with safe display text"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    error: ProtocolError = ProtocolError("safe protocol failure")
    error.add_note("safe diagnostic for traceback only")
    log_exception: Mock = Mock()

    async def fake_read(selected: AccountConfig) -> Dict[str, object]:
        raise error

    monkeypatch.setattr(limits, "read_account_limits", fake_read)
    monkeypatch.setattr(limits.logger, "exception", log_exception)
    expected: AccountStatus = AccountStatus(account, {}, "safe protocol failure")

    # ACT
    actual: AccountStatus = asyncio.run(limits.get_account_status(account))

    # ASSERT
    assert actual == expected
    assert "diagnostic" not in actual.error_message
    log_exception.assert_called_once_with(
        "Failed to fetch limits: account_slug=%s",
        "personal",
    )


def test_sensitive_app_server_stderr_reaches_only_redacted_log_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify transport diagnostics are redacted in logs and absent from UI"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    account.codex_home.mkdir(parents=True)
    (account.codex_home / limits.AUTH_FILE_NAME).write_bytes(b"opaque")
    stdin: FakeStdin = FakeStdin()
    stdout: FakeStdout = FakeStdout([b"not-json\n"])
    process: FakeProcess = FakeProcess(stdin=stdin, stdout=stdout)
    log_stream: io.StringIO = io.StringIO()
    handler: logging.StreamHandler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    original_sensitive_text: str = "Authorization synthetic diagnostic"

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        stderr_file: BinaryIO = kwargs["stderr"]
        stderr_file.write(original_sensitive_text.encode("utf-8"))
        return process

    monkeypatch.setattr(limits.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(limits.logger, "handlers", [handler])
    monkeypatch.setattr(limits.logger, "level", logging.ERROR)
    monkeypatch.setattr(limits.logger, "propagate", False)
    refreshed_at: datetime = datetime(2026, 7, 21, 9, 5, tzinfo=timezone.utc)

    # ACT
    status: AccountStatus = asyncio.run(limits.get_account_status(account))
    screen: str = render_screen([status], refreshed_at, 300)
    log_text: str = log_stream.getvalue()

    # ASSERT
    assert status == AccountStatus(account, {}, "Codex App Server returned invalid JSON")
    assert process.terminate_calls == 1
    assert "account_slug=personal" in log_text
    assert limits.REDACTED_STDERR_TEXT in log_text
    assert original_sensitive_text not in log_text
    assert limits.REDACTED_STDERR_TEXT not in screen
    assert original_sensitive_text not in screen
    handler.close()


def test_get_account_status_hides_unexpected_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify unknown account failures expose only the generic message"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    log_exception: Mock = Mock()

    async def fake_read(selected: AccountConfig) -> Dict[str, object]:
        raise RuntimeError("internal details")

    monkeypatch.setattr(limits, "read_account_limits", fake_read)
    monkeypatch.setattr(limits.logger, "exception", log_exception)
    expected: AccountStatus = AccountStatus(account, {}, "Unexpected account error; see app.log")

    # ACT
    actual: AccountStatus = asyncio.run(limits.get_account_status(account))

    # ASSERT
    assert actual == expected
    log_exception.assert_called_once_with(
        "Unexpected limit error: account_slug=%s",
        "personal",
    )


def test_get_account_status_propagates_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify account cancellation is never converted into display state"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    log_exception: Mock = Mock()

    async def fake_read(selected: AccountConfig) -> Dict[str, object]:
        raise asyncio.CancelledError()

    monkeypatch.setattr(limits, "read_account_limits", fake_read)
    monkeypatch.setattr(limits.logger, "exception", log_exception)

    # ACT
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(limits.get_account_status(account))

    # ASSERT
    log_exception.assert_not_called()


def test_get_account_status_does_not_expose_unused_raw_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify raw response siblings never reach screen or logs"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    synthetic_sensitive_value: str = "opaque-sensitive-test-value"
    result: Dict[str, object] = {
        "unused": synthetic_sensitive_value,
        "rateLimits": {"primary": None, "secondary": None},
    }
    log_exception: Mock = Mock()

    async def fake_read(selected: AccountConfig) -> Dict[str, object]:
        return result

    monkeypatch.setattr(limits, "read_account_limits", fake_read)
    monkeypatch.setattr(limits.logger, "exception", log_exception)

    # ACT
    status: AccountStatus = asyncio.run(limits.get_account_status(account))
    screen: str = render_screen([status], datetime(2026, 7, 21, tzinfo=timezone.utc), 300)

    # ASSERT
    assert synthetic_sensitive_value not in screen
    log_exception.assert_not_called()


@pytest.mark.parametrize(
    "field_name",
    [limits.USED_PERCENT_FIELD, limits.WINDOW_DURATION_FIELD],
)
def test_get_account_status_does_not_log_malformed_numeric_values(
    field_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify malformed protocol numbers cannot echo raw values into logs"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    synthetic_sensitive_value: str = "opaque-sensitive-test-value"
    window: Dict[str, object] = {
        "usedPercent": 25,
        "windowDurationMins": 300,
    }
    window[field_name] = synthetic_sensitive_value
    result: Dict[str, object] = {
        "rateLimits": {
            "primary": window,
            "secondary": None,
        },
    }
    expected_error: str = "primary usedPercent must be a number"
    if field_name == limits.WINDOW_DURATION_FIELD:
        expected_error = "primary windowDurationMins must be a positive integer"
    log_stream: io.StringIO = io.StringIO()
    handler: logging.StreamHandler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter("%(message)s"))

    async def fake_read(selected: AccountConfig) -> Dict[str, object]:
        return result

    monkeypatch.setattr(limits, "read_account_limits", fake_read)
    monkeypatch.setattr(limits.logger, "handlers", [handler])
    monkeypatch.setattr(limits.logger, "level", logging.ERROR)
    monkeypatch.setattr(limits.logger, "propagate", False)

    # ACT
    status: AccountStatus = asyncio.run(limits.get_account_status(account))
    screen: str = render_screen([status], datetime(2026, 7, 21, tzinfo=timezone.utc), 300)
    log_text: str = log_stream.getvalue()

    # ASSERT
    assert status == AccountStatus(account, {}, expected_error)
    assert synthetic_sensitive_value not in screen
    assert synthetic_sensitive_value not in log_text
    handler.close()


def test_parallel_account_isolation_preserves_success_and_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify one account error neither cancels nor reorders another account"""

    # ARRANGE
    personal: AccountConfig = make_account(tmp_path, "personal", "Personal")
    work: AccountConfig = make_account(tmp_path, "work", "Work")
    personal_started: asyncio.Event
    work_started: asyncio.Event
    log_exception: Mock = Mock()

    async def gather_statuses() -> List[AccountStatus]:
        nonlocal personal_started
        nonlocal work_started
        personal_started = asyncio.Event()
        work_started = asyncio.Event()

        async def fake_read(account: AccountConfig) -> Dict[str, object]:
            if account.slug == "personal":
                personal_started.set()
                await work_started.wait()
                raise ProtocolError("personal unavailable")
            work_started.set()
            await personal_started.wait()
            return {
                "rateLimits": {
                    "primary": {"usedPercent": 20, "windowDurationMins": 300},
                    "secondary": None,
                },
            }

        monkeypatch.setattr(limits, "read_account_limits", fake_read)
        return list(
            await asyncio.gather(
                *(limits.get_account_status(account) for account in [personal, work]),
            ),
        )

    monkeypatch.setattr(limits.logger, "exception", log_exception)
    expected: List[AccountStatus] = [
        AccountStatus(personal, {}, "personal unavailable"),
        AccountStatus(work, {300: LimitWindow(300, 20.0, None)}, None),
    ]

    # ACT
    actual: List[AccountStatus] = asyncio.run(gather_statuses())

    # ASSERT
    assert actual == expected
    log_exception.assert_called_once_with(
        "Failed to fetch limits: account_slug=%s",
        "personal",
    )


def test_run_monitor_fetches_renders_writes_then_sleeps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify immediate parallel refresh precedes render and configured sleep"""

    # ARRANGE
    accounts: List[AccountConfig] = [
        make_account(tmp_path, "personal", "Personal"),
        make_account(tmp_path, "work", "Work"),
    ]
    config: AppConfig = AppConfig(refresh_seconds=300, accounts=accounts)
    events: List[str] = []
    fixed_now: datetime = datetime(2026, 7, 21, 9, 5, tzinfo=timezone.utc)
    datetime_boundary: Mock = Mock()
    datetime_boundary.now.return_value = fixed_now

    async def fake_status(account: AccountConfig) -> AccountStatus:
        events.append(f"fetch:{account.slug}")
        return AccountStatus(account, {}, None)

    def fake_render(
        statuses: Sequence[AccountStatus],
        refreshed_at: datetime,
        refresh_seconds: int,
    ) -> str:
        events.append("render")
        assert list(statuses) == [AccountStatus(account, {}, None) for account in accounts]
        assert refreshed_at == fixed_now
        assert refresh_seconds == 300
        return "snapshot"

    def fake_write(screen: str) -> None:
        events.append("write")
        assert screen == "snapshot"

    async def fake_sleep(seconds: int) -> None:
        events.append(f"sleep:{seconds}")
        raise StopMonitor()

    monkeypatch.setattr(limits, "get_account_status", fake_status)
    monkeypatch.setattr(limits, "render_screen", fake_render)
    monkeypatch.setattr(limits, "write_screen", fake_write)
    monkeypatch.setattr(limits.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(limits, "datetime", datetime_boundary)

    # ACT
    with pytest.raises(StopMonitor):
        asyncio.run(limits.run_monitor(config))

    # ASSERT
    assert events == ["fetch:personal", "fetch:work", "render", "write", "sleep:300"]


def test_run_monitor_does_not_reuse_stale_success_after_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify every refresh renders only its newly fetched state"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    config: AppConfig = AppConfig(refresh_seconds=300, accounts=[account])
    fetch_count: int = 0
    sleep_count: int = 0
    screens: List[str] = []
    fixed_now: datetime = datetime(2026, 7, 21, 9, 5, tzinfo=timezone.utc)
    datetime_boundary: Mock = Mock()
    datetime_boundary.now.return_value = fixed_now

    async def fake_status(selected: AccountConfig) -> AccountStatus:
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            return AccountStatus(selected, {300: LimitWindow(300, 25.0, None)}, None)
        return AccountStatus(selected, {}, "new failure")

    def capture_screen(screen: str) -> None:
        screens.append(screen)

    async def fake_sleep(seconds: int) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            raise StopMonitor()

    monkeypatch.setattr(limits, "get_account_status", fake_status)
    monkeypatch.setattr(limits, "write_screen", capture_screen)
    monkeypatch.setattr(limits.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(limits, "datetime", datetime_boundary)

    # ACT
    with pytest.raises(StopMonitor):
        asyncio.run(limits.run_monitor(config))

    # ASSERT
    assert len(screens) == 2
    assert "5 часов: [███████████████░░░░░] 75% осталось" in screens[0]
    assert "Ошибка: new failure" in screens[1]
    assert "5 часов:" not in screens[1]


def test_configure_logging_creates_one_protected_error_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify application logging permissions and exact handler configuration"""

    # ARRANGE
    state_root, _ = set_state_roots(monkeypatch, tmp_path)

    # ACT
    limits.configure_logging()

    # ASSERT
    if limits.sys.platform != limits.WINDOWS_PLATFORM:
        assert stat.S_IMODE(state_root.stat().st_mode) == limits.DIRECTORY_MODE
        assert stat.S_IMODE(limits.LOG_PATH.stat().st_mode) == limits.FILE_MODE
    assert len(limits.logger.handlers) == 1
    handler: logging.Handler = limits.logger.handlers[0]
    assert isinstance(handler, logging.FileHandler)
    assert handler.level == logging.ERROR
    assert handler.encoding == "utf-8"
    assert handler.formatter is not None
    assert handler.formatter._fmt == limits.LOG_FORMAT
    assert limits.logger.level == logging.ERROR
    assert limits.logger.propagate is False
    handler.close()
    limits.logger.handlers.clear()


def test_configure_logging_chains_storage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify log preparation failures become safe configuration errors"""

    # ARRANGE
    set_state_roots(monkeypatch, tmp_path)

    def fail_mkdir(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic log failure")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    # ACT
    with pytest.raises(ConfigError) as caught:
        limits.configure_logging()

    # ASSERT
    assert str(caught.value) == "Cannot prepare application log"
    assert isinstance(caught.value.__cause__, OSError)


def test_ensure_codex_available_accepts_found_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a discovered Codex executable passes startup validation"""

    # ARRANGE
    which: Mock = Mock(return_value="/usr/bin/codex")
    monkeypatch.setattr(limits.shutil, "which", which)

    # ACT
    limits.ensure_codex_available()

    # ASSERT
    which.assert_called_once_with("codex")


def test_ensure_codex_available_rejects_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify missing Codex CLI produces the normative startup error"""

    # ARRANGE
    monkeypatch.setattr(limits.shutil, "which", Mock(return_value=None))

    # ACT
    with pytest.raises(ConfigError) as caught:
        limits.ensure_codex_available()

    # ASSERT
    assert str(caught.value) == "Codex CLI is not available in PATH"


def test_get_codex_command_resolves_windows_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Windows uses the executable path resolved through PATHEXT"""

    # ARRANGE
    codex_path: str = r"C:\Users\tester\AppData\Roaming\npm\codex.CMD"
    which: Mock = Mock(return_value=codex_path)
    monkeypatch.setattr(limits.sys, "platform", limits.WINDOWS_PLATFORM)
    monkeypatch.setattr(limits.shutil, "which", which)

    # ACT
    actual: str = limits.get_codex_command()

    # ASSERT
    assert actual == codex_path
    which.assert_called_once_with(limits.CODEX_COMMAND)


def test_get_account_by_slug_returns_exact_match(tmp_path: Path) -> None:
    """Verify account lookup preserves the configured object"""

    # ARRANGE
    accounts: List[AccountConfig] = [
        make_account(tmp_path, "personal", "Personal"),
        make_account(tmp_path, "work", "Work"),
    ]

    # ACT
    actual: AccountConfig = limits.get_account_by_slug(accounts, "work")

    # ASSERT
    assert actual == accounts[1]


def test_get_account_by_slug_rejects_unknown_slug(tmp_path: Path) -> None:
    """Verify unknown login selection produces a configuration error"""

    # ARRANGE
    accounts: List[AccountConfig] = [make_account(tmp_path)]

    # ACT
    with pytest.raises(ConfigError) as caught:
        limits.get_account_by_slug(accounts, "missing")

    # ASSERT
    assert str(caught.value) == "Unknown account slug: missing"


def test_parse_arguments_uses_only_normative_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify CLI defaults select config.toml and monitoring"""

    # ARRANGE
    monkeypatch.setattr(limits.sys, "argv", ["chatgpt_limits.py"])
    expected: argparse.Namespace = argparse.Namespace(config=Path("config.toml"), login=None)

    # ACT
    actual: argparse.Namespace = limits.parse_arguments()

    # ASSERT
    assert actual == expected


def test_parse_arguments_accepts_explicit_config_and_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify both supported options are parsed exactly"""

    # ARRANGE
    monkeypatch.setattr(
        limits.sys,
        "argv",
        ["chatgpt_limits.py", "--config", "other.toml", "--login", "work"],
    )
    expected: argparse.Namespace = argparse.Namespace(config=Path("other.toml"), login="work")

    # ACT
    actual: argparse.Namespace = limits.parse_arguments()

    # ASSERT
    assert actual == expected


@pytest.mark.parametrize(
    "arguments",
    [
        ["--login"],
        ["--config"],
        ["--unknown"],
        ["--conf", "other.toml"],
        ["--log", "personal"],
    ],
)
def test_parse_arguments_rejects_invalid_cli_with_code_two(
    arguments: List[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify argparse owns missing values, unknown options, and abbreviations"""

    # ARRANGE
    monkeypatch.setattr(limits.sys, "argv", ["chatgpt_limits.py"] + arguments)

    # ACT
    with pytest.raises(SystemExit) as caught:
        limits.parse_arguments()

    # ASSERT
    assert caught.value.code == 2


def test_run_application_login_branch_has_single_preparation_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify login delegates storage preparation only to login_account"""

    # ARRANGE
    account: AccountConfig = make_account(tmp_path)
    config: AppConfig = AppConfig(300, [account])
    arguments: argparse.Namespace = argparse.Namespace(config=tmp_path / "config.toml", login="personal")
    events: List[str] = []

    def fake_load(path: Path) -> AppConfig:
        events.append(f"load:{path.name}")
        return config

    def fake_ensure() -> None:
        events.append("ensure")

    async def fake_login(selected: AccountConfig) -> None:
        events.append(f"login:{selected.slug}")

    def forbidden_prepare(selected: AccountConfig) -> None:
        raise AssertionError("run_application prepared login storage directly")

    monkeypatch.setattr(limits, "load_config", fake_load)
    monkeypatch.setattr(limits, "ensure_codex_available", fake_ensure)
    monkeypatch.setattr(limits, "login_account", fake_login)
    monkeypatch.setattr(limits, "prepare_account_home", forbidden_prepare)

    # ACT
    limits.run_application(arguments)

    # ASSERT
    assert events == ["load:config.toml", "ensure", "login:personal"]


def test_run_application_monitor_prepares_every_account_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify monitor startup protects all account homes in order"""

    # ARRANGE
    accounts: List[AccountConfig] = [
        make_account(tmp_path, "personal", "Personal"),
        make_account(tmp_path, "work", "Work"),
    ]
    config: AppConfig = AppConfig(300, accounts)
    arguments: argparse.Namespace = argparse.Namespace(config=tmp_path / "config.toml", login=None)
    events: List[str] = []

    def fake_load(path: Path) -> AppConfig:
        events.append("load")
        return config

    def fake_ensure() -> None:
        events.append("ensure")

    def fake_prepare(account: AccountConfig) -> None:
        events.append(f"prepare:{account.slug}")

    async def fake_monitor(selected: AppConfig) -> None:
        assert selected == config
        events.append("monitor")

    monkeypatch.setattr(limits, "load_config", fake_load)
    monkeypatch.setattr(limits, "ensure_codex_available", fake_ensure)
    monkeypatch.setattr(limits, "prepare_account_home", fake_prepare)
    monkeypatch.setattr(limits, "run_monitor", fake_monitor)

    # ACT
    limits.run_application(arguments)

    # ASSERT
    assert events == ["load", "ensure", "prepare:personal", "prepare:work", "monitor"]


def test_main_returns_success_without_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a normal application return maps to exit code zero"""

    # ARRANGE
    arguments: argparse.Namespace = argparse.Namespace(config=Path("config.toml"), login=None)
    stderr: io.StringIO = io.StringIO()
    log_exception: Mock = Mock()
    monkeypatch.setattr(limits, "parse_arguments", Mock(return_value=arguments))
    monkeypatch.setattr(limits, "configure_logging", Mock(return_value=None))
    monkeypatch.setattr(limits, "run_application", Mock(return_value=None))
    monkeypatch.setattr(limits.sys, "stderr", stderr)
    monkeypatch.setattr(limits.logger, "exception", log_exception)

    # ACT
    exit_code: int = limits.main()

    # ASSERT
    assert exit_code == limits.SUCCESS_EXIT_CODE
    assert stderr.getvalue() == ""
    log_exception.assert_not_called()


def test_main_logs_known_startup_error_and_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify known startup errors retain traceback logging and safe stderr"""

    # ARRANGE
    arguments: argparse.Namespace = argparse.Namespace(config=Path("config.toml"), login=None)
    stderr: io.StringIO = io.StringIO()
    log_exception: Mock = Mock()

    def fail_run(selected: argparse.Namespace) -> None:
        raise ConfigError("known startup failure")

    monkeypatch.setattr(limits, "parse_arguments", Mock(return_value=arguments))
    monkeypatch.setattr(limits, "configure_logging", Mock(return_value=None))
    monkeypatch.setattr(limits, "run_application", fail_run)
    monkeypatch.setattr(limits.sys, "stderr", stderr)
    monkeypatch.setattr(limits.logger, "exception", log_exception)

    # ACT
    exit_code: int = limits.main()

    # ASSERT
    assert exit_code == limits.ERROR_EXIT_CODE
    assert stderr.getvalue() == "Error: known startup failure\n"
    log_exception.assert_called_once_with("Application failed")


def test_main_hides_unexpected_startup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify unexpected startup details stay out of stderr"""

    # ARRANGE
    arguments: argparse.Namespace = argparse.Namespace(config=Path("config.toml"), login=None)
    stderr: io.StringIO = io.StringIO()
    log_exception: Mock = Mock()

    def fail_run(selected: argparse.Namespace) -> None:
        raise RuntimeError("internal startup details")

    monkeypatch.setattr(limits, "parse_arguments", Mock(return_value=arguments))
    monkeypatch.setattr(limits, "configure_logging", Mock(return_value=None))
    monkeypatch.setattr(limits, "run_application", fail_run)
    monkeypatch.setattr(limits.sys, "stderr", stderr)
    monkeypatch.setattr(limits.logger, "exception", log_exception)

    # ACT
    exit_code: int = limits.main()

    # ASSERT
    assert exit_code == limits.ERROR_EXIT_CODE
    assert stderr.getvalue() == "Error: Unexpected application failure; see app.log\n"
    assert "internal startup details" not in stderr.getvalue()
    log_exception.assert_called_once_with("Unexpected application failure")


def test_main_maps_keyboard_interrupt_to_code_130(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Ctrl+C returns the conventional interruption code without output"""

    # ARRANGE
    arguments: argparse.Namespace = argparse.Namespace(config=Path("config.toml"), login=None)
    stderr: io.StringIO = io.StringIO()
    log_exception: Mock = Mock()

    def interrupt_run(selected: argparse.Namespace) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(limits, "parse_arguments", Mock(return_value=arguments))
    monkeypatch.setattr(limits, "configure_logging", Mock(return_value=None))
    monkeypatch.setattr(limits, "run_application", interrupt_run)
    monkeypatch.setattr(limits.sys, "stderr", stderr)
    monkeypatch.setattr(limits.logger, "exception", log_exception)

    # ACT
    exit_code: int = limits.main()

    # ASSERT
    assert exit_code == limits.INTERRUPTED_EXIT_CODE
    assert stderr.getvalue() == ""
    log_exception.assert_not_called()


def test_main_reports_logging_setup_error_without_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify failure to create the log is written only to safe stderr"""

    # ARRANGE
    arguments: argparse.Namespace = argparse.Namespace(config=Path("config.toml"), login=None)
    stderr: io.StringIO = io.StringIO()
    run_application: Mock = Mock()
    log_exception: Mock = Mock()

    def fail_logging() -> None:
        raise ConfigError("Cannot prepare application log")

    monkeypatch.setattr(limits, "parse_arguments", Mock(return_value=arguments))
    monkeypatch.setattr(limits, "configure_logging", fail_logging)
    monkeypatch.setattr(limits, "run_application", run_application)
    monkeypatch.setattr(limits.sys, "stderr", stderr)
    monkeypatch.setattr(limits.logger, "exception", log_exception)

    # ACT
    exit_code: int = limits.main()

    # ASSERT
    assert exit_code == limits.ERROR_EXIT_CODE
    assert stderr.getvalue() == "Error: Cannot prepare application log\n"
    run_application.assert_not_called()
    log_exception.assert_not_called()


def test_main_propagates_asyncio_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify cancellation is never converted to an exit status internally"""

    # ARRANGE
    arguments: argparse.Namespace = argparse.Namespace(config=Path("config.toml"), login=None)

    def cancel_run(selected: argparse.Namespace) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(limits, "parse_arguments", Mock(return_value=arguments))
    monkeypatch.setattr(limits, "configure_logging", Mock(return_value=None))
    monkeypatch.setattr(limits, "run_application", cancel_run)

    # ACT
    with pytest.raises(asyncio.CancelledError):
        limits.main()

    # ASSERT
    assert True


def test_example_config_matches_normative_contents() -> None:
    """Verify the committed example has only the two documented accounts"""

    # ARRANGE
    project_root: Path = Path(__file__).resolve().parents[1]
    expected: str = (
        'refresh_seconds = 300\n\n[[accounts]]\nslug = "personal"\nname = "Personal Pro"\n\n'
        '[[accounts]]\nslug = "work"\nname = "Work Pro"\n'
    )

    # ACT
    actual: str = (project_root / "config.example.toml").read_text(encoding="utf-8")

    # ASSERT
    assert actual == expected


def test_project_contains_only_allowed_implementation_artifacts() -> None:
    """Verify implementation scope remains exactly the approved three files"""

    # ARRANGE
    project_root: Path = Path(__file__).resolve().parents[1]
    expected: Set[Path] = {
        Path("chatgpt_limits.py"),
        Path("config.example.toml"),
        Path("tests/test_chatgpt_limits.py"),
    }

    # ACT
    actual: Set[Path] = {
        path.relative_to(project_root)
        for path in project_root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".toml"}
        and path.relative_to(project_root) != limits.DEFAULT_CONFIG_PATH
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
    }

    # ASSERT
    assert actual == expected


def test_production_source_obeys_static_scope_and_style_contract() -> None:
    """Verify imports, annotations, lines, assignments, and public docstrings"""

    # ARRANGE
    project_root: Path = Path(__file__).resolve().parents[1]
    source_path: Path = project_root / "chatgpt_limits.py"
    source: str = source_path.read_text(encoding="utf-8")
    tree: ast.Module = ast.parse(source)
    allowed_modules: Set[str] = {
        "argparse",
        "asyncio",
        "dataclasses",
        "datetime",
        "json",
        "logging",
        "math",
        "os",
        "pathlib",
        "re",
        "shutil",
        "sys",
        "tempfile",
        "tomllib",
        "typing",
    }
    forbidden_fragments: Sequence[str] = (
        "print(",
        "getattr",
        "setattr",
        "__dict__",
        "__call__",
        "def __new__",
        "list[",
        "dict[",
        "set[",
        "tuple[",
        "aiohttp",
        "requests",
        "httpx",
        "rich",
        "click",
        "typer",
        "pydantic",
        "playwright",
        "selenium",
    )

    # ACT
    imported_modules: Set[str] = set()
    multiple_assignments: List[ast.Assign] = []
    undocumented_public_nodes: List[str] = []
    punctuated_descriptions: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module.split(".")[0])
        if isinstance(node, ast.Assign) and len(node.targets) != 1:
            multiple_assignments.append(node)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            docstring: Optional[str] = ast.get_docstring(node)
            if docstring is None:
                undocumented_public_nodes.append(node.name)
            elif docstring.splitlines()[0].endswith("."):
                punctuated_descriptions.append(node.name)

    # ASSERT
    assert imported_modules <= allowed_modules
    assert multiple_assignments == []
    assert undocumented_public_nodes == []
    assert punctuated_descriptions == []
    assert all(len(line) <= 120 for line in source.splitlines())
    assert all(fragment not in source for fragment in forbidden_fragments)
    assert all(not line.lstrip().startswith("#") for line in source.splitlines())
