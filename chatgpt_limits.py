import argparse
import asyncio
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Pattern, Sequence, Set


APP_NAME: str = "chatgpt-limits"
APP_TITLE: str = "ChatGPT Limits"
APP_VERSION: str = "1.0.0"
SCREEN_TITLE: str = "ChatGPT Pro / Codex limits"
FIVE_HOUR_LABEL: str = "5 часов"
WEEK_LABEL: str = "Неделя"
NO_DATA_TEXT: str = "нет данных"
UNKNOWN_RESET_TEXT: str = "сброс неизвестен"
STOP_TEXT: str = "Остановка: Ctrl+C"
CODEX_COMMAND: str = "codex"
APP_SERVER_COMMAND: str = "app-server"
LOGIN_COMMAND: str = "login"
WINDOWS_PLATFORM: str = "win32"
CODEX_HOME_ENV_NAME: str = "CODEX_HOME"
CONFIG_OPTION: str = "--config"
LOGIN_OPTION: str = "--login"
DEFAULT_CONFIG_PATH: Path = Path("config.toml")
STATE_ROOT: Path = Path.home() / ".chatgpt-limits"
ACCOUNTS_ROOT: Path = STATE_ROOT / "accounts"
LOG_PATH: Path = STATE_ROOT / "app.log"
AUTH_FILE_NAME: str = "auth.json"
CODEX_CONFIG_FILE_NAME: str = "config.toml"
CODEX_CONFIG_CONTENT: str = 'cli_auth_credentials_store = "file"\n'
INITIALIZE_METHOD: str = "initialize"
INITIALIZED_METHOD: str = "initialized"
RATE_LIMITS_METHOD: str = "account/rateLimits/read"
RATE_LIMITS_FIELD: str = "rateLimits"
RATE_LIMITS_BY_ID_FIELD: str = "rateLimitsByLimitId"
CODEX_LIMIT_ID: str = "codex"
PRIMARY_FIELD: str = "primary"
SECONDARY_FIELD: str = "secondary"
USED_PERCENT_FIELD: str = "usedPercent"
WINDOW_DURATION_FIELD: str = "windowDurationMins"
RESETS_AT_FIELD: str = "resetsAt"
INITIALIZE_REQUEST_ID: int = 1
RATE_LIMITS_REQUEST_ID: int = 2
REQUEST_TIMEOUT_SECONDS: int = 30
SHUTDOWN_TIMEOUT_SECONDS: int = 5
MAX_STDERR_BYTES: int = 16384
FIVE_HOUR_MINUTES: int = 300
WEEK_MINUTES: int = 10080
MIN_PERCENT: float = 0.0
MAX_PERCENT: float = 100.0
PROGRESS_BAR_WIDTH: int = 20
PROGRESS_FILLED_CHAR: str = "█"
PROGRESS_EMPTY_CHAR: str = "░"
SECONDS_PER_MINUTE: int = 60
INTERRUPTED_EXIT_CODE: int = 130
SUCCESS_EXIT_CODE: int = 0
ERROR_EXIT_CODE: int = 1
DIRECTORY_MODE: int = 0o700
FILE_MODE: int = 0o600
CLEAR_SCREEN: str = "\x1b[2J\x1b[H"
DATETIME_FORMAT: str = "%Y-%m-%d %H:%M:%S %Z"
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(message)s"
CLI_DESCRIPTION: str = "Display ChatGPT Pro Codex rate limits"
REDACTED_STDERR_TEXT: str = "<stderr omitted because it may contain credentials>"
STDERR_NOTE_PREFIX: str = "Sanitized Codex App Server stderr"
SLUG_PATTERN: Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TOP_LEVEL_CONFIG_KEYS: Set[str] = {"refresh_seconds", "accounts"}
ACCOUNT_CONFIG_KEYS: Set[str] = {"slug", "name"}
SENSITIVE_STDERR_MARKERS: Sequence[str] = (
    "authorization",
    "bearer ",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "token=",
    "eyj",
)


logger: logging.Logger = logging.getLogger(APP_NAME)


class LimitsError(Exception):
    """Base application error"""


class ConfigError(LimitsError):
    """Report invalid application configuration"""


class LoginError(LimitsError):
    """Report a failed account login"""


class AppServerError(LimitsError):
    """Report Codex App Server communication failure"""


class ProtocolError(AppServerError):
    """Report an invalid Codex App Server response"""


@dataclass(frozen=True)
class AccountConfig:
    """Describe one configured ChatGPT account

    Attributes:
        slug: Stable account key used by CLI and filesystem
        name: User-facing account name
        codex_home: Isolated Codex home directory
    """

    slug: str
    name: str
    codex_home: Path


@dataclass(frozen=True)
class AppConfig:
    """Describe validated application configuration

    Attributes:
        refresh_seconds: Delay between completed refreshes
        accounts: Accounts displayed in configured order
    """

    refresh_seconds: int
    accounts: List[AccountConfig]


@dataclass(frozen=True)
class LimitWindow:
    """Describe one rate-limit window

    Attributes:
        duration_minutes: Window duration reported by Codex
        used_percent: Usage percentage reported by Codex
        resets_at: Local reset time when provided
    """

    duration_minutes: int
    used_percent: float
    resets_at: Optional[datetime]


@dataclass(frozen=True)
class AccountStatus:
    """Describe display state for one account

    Attributes:
        account: Account configuration
        windows: Valid windows keyed by duration in minutes
        error_message: Safe user-facing error or None
    """

    account: AccountConfig
    windows: Dict[int, LimitWindow]
    error_message: Optional[str]


def parse_account(
    value: object,
    slugs: Set[str],
    names: Set[str],
) -> AccountConfig:
    """Validate one account configuration

    Args:
        value: External TOML account value
        slugs: Slugs already used by preceding accounts
        names: Names already used by preceding accounts

    Returns:
        Validated account configuration

    Raises:
        ConfigError: If the account value is invalid
    """
    if not isinstance(value, dict):
        raise ConfigError("Every account must be a TOML table")

    account_keys: Set[str] = set(value)
    if account_keys != ACCOUNT_CONFIG_KEYS:
        raise ConfigError("Account has missing or unknown fields")

    slug_value: object = value.get("slug")
    name_value: object = value.get("name")
    if not isinstance(slug_value, str):
        raise ConfigError("Account slug must be a string")
    if not isinstance(name_value, str):
        raise ConfigError("Account name must be a string")

    slug: str = slug_value.strip()
    name: str = name_value.strip()
    if SLUG_PATTERN.fullmatch(slug) is None:
        raise ConfigError(f"Invalid account slug: {slug}")
    if not name:
        raise ConfigError("Account name must not be empty")
    if slug in slugs:
        raise ConfigError(f"Duplicate account slug: {slug}")
    if name in names:
        raise ConfigError(f"Duplicate account name: {name}")

    codex_home: Path = ACCOUNTS_ROOT / slug
    return AccountConfig(
        slug=slug,
        name=name,
        codex_home=codex_home,
    )


def load_config(path: Path) -> AppConfig:
    """Load and validate monitor configuration

    Args:
        path: TOML configuration path

    Returns:
        Validated application configuration

    Raises:
        ConfigError: If the file is missing or invalid
    """
    try:
        content: bytes = path.read_bytes()
        data: Dict[str, object] = tomllib.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"Cannot read configuration: {path}") from error

    config_keys: Set[str] = set(data)
    if config_keys != TOP_LEVEL_CONFIG_KEYS:
        raise ConfigError("Configuration has missing or unknown fields")

    refresh_value: object = data.get("refresh_seconds")
    if isinstance(refresh_value, bool):
        raise ConfigError("refresh_seconds must be a positive integer")
    if not isinstance(refresh_value, int):
        raise ConfigError("refresh_seconds must be a positive integer")
    if refresh_value <= 0:
        raise ConfigError("refresh_seconds must be a positive integer")

    accounts_value: object = data.get("accounts")
    if not isinstance(accounts_value, list):
        raise ConfigError("accounts must be a non-empty array")
    if not accounts_value:
        raise ConfigError("accounts must be a non-empty array")

    accounts: List[AccountConfig] = []
    slugs: Set[str] = set()
    names: Set[str] = set()

    for account_value in accounts_value:
        account: AccountConfig = parse_account(account_value, slugs, names)
        accounts.append(account)
        slugs.add(account.slug)
        names.add(account.name)

    return AppConfig(
        refresh_seconds=refresh_value,
        accounts=accounts,
    )


def prepare_account_home(account: AccountConfig) -> None:
    """Prepare isolated Codex storage for an account

    Args:
        account: Account whose storage must be prepared

    Raises:
        LoginError: If storage cannot be prepared
    """
    try:
        STATE_ROOT.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        ACCOUNTS_ROOT.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        account.codex_home.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        os.chmod(STATE_ROOT, DIRECTORY_MODE)
        os.chmod(ACCOUNTS_ROOT, DIRECTORY_MODE)
        os.chmod(account.codex_home, DIRECTORY_MODE)

        config_path: Path = account.codex_home / CODEX_CONFIG_FILE_NAME
        config_path.write_text(CODEX_CONFIG_CONTENT, encoding="utf-8")
        os.chmod(config_path, FILE_MODE)

        auth_path: Path = account.codex_home / AUTH_FILE_NAME
        if auth_path.exists():
            os.chmod(auth_path, FILE_MODE)
    except OSError as error:
        raise LoginError(f"Cannot prepare account storage: {account.slug}") from error


def build_codex_environment(account: AccountConfig) -> Dict[str, str]:
    """Build environment for one isolated Codex process

    Args:
        account: Account whose Codex home must be selected

    Returns:
        Process environment containing isolated CODEX_HOME
    """
    environment: Dict[str, str] = os.environ.copy()
    environment[CODEX_HOME_ENV_NAME] = str(account.codex_home)
    return environment


async def login_account(account: AccountConfig) -> None:
    """Run official ChatGPT login for one account

    Args:
        account: Account selected by slug

    Raises:
        LoginError: If Codex login fails
    """
    prepare_account_home(account)
    environment: Dict[str, str] = build_codex_environment(account)

    try:
        process: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
            get_codex_command(),
            LOGIN_COMMAND,
            env=environment,
        )
        return_code: int = await process.wait()
    except asyncio.CancelledError:
        raise
    except OSError as error:
        raise LoginError(f"Cannot start Codex login: {account.slug}") from error

    if return_code != 0:
        raise LoginError(f"Codex login failed for {account.slug}")

    auth_path: Path = account.codex_home / AUTH_FILE_NAME
    if not auth_path.is_file():
        raise LoginError(f"Codex did not create credentials for {account.slug}")

    try:
        os.chmod(auth_path, FILE_MODE)
    except OSError as error:
        raise LoginError(f"Cannot protect credentials for {account.slug}") from error


def build_initialize_request() -> Dict[str, object]:
    """Build the Codex App Server initialize request

    Returns:
        Initialize request
    """
    client_info: Dict[str, str] = {
        "name": APP_NAME,
        "title": APP_TITLE,
        "version": APP_VERSION,
    }
    initialize_params: Dict[str, object] = {
        "clientInfo": client_info,
    }
    return {
        "method": INITIALIZE_METHOD,
        "id": INITIALIZE_REQUEST_ID,
        "params": initialize_params,
    }


def build_initialized_notification() -> Dict[str, object]:
    """Build the Codex App Server initialized notification

    Returns:
        Initialized notification
    """
    return {
        "method": INITIALIZED_METHOD,
        "params": {},
    }


def build_rate_limits_request() -> Dict[str, object]:
    """Build the Codex App Server rate-limit request

    Returns:
        Rate-limit request
    """
    return {
        "method": RATE_LIMITS_METHOD,
        "id": RATE_LIMITS_REQUEST_ID,
    }


async def write_rpc_message(
    stdin: asyncio.StreamWriter,
    message: Dict[str, object],
) -> None:
    """Write and flush one JSONL message

    Args:
        stdin: App Server stdin stream
        message: Message to serialize
    """
    payload: str = json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    stdin.write(payload.encode("utf-8"))
    stdin.write(b"\n")
    await stdin.drain()


async def read_rpc_result(
    stdout: asyncio.StreamReader,
    request_id: int,
) -> Dict[str, object]:
    """Read one matching JSONL response from Codex App Server

    Args:
        stdout: App Server stdout stream
        request_id: Expected response identifier

    Returns:
        RPC result object

    Raises:
        AppServerError: If the response times out
        ProtocolError: If the stream or JSON response is invalid
    """
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            while True:
                line: bytes = await stdout.readline()
                if not line:
                    raise ProtocolError("Codex App Server closed stdout")

                try:
                    line_text: str = line.decode("utf-8")
                    message: object = json.loads(line_text)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ProtocolError("Codex App Server returned invalid JSON") from error

                if not isinstance(message, dict):
                    raise ProtocolError("Codex App Server returned a non-object message")
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise AppServerError(f"Codex App Server rejected request {request_id}")

                result: object = message.get("result")
                if not isinstance(result, dict):
                    raise ProtocolError("Codex App Server response has no result object")
                return result
    except asyncio.CancelledError:
        raise
    except TimeoutError as error:
        raise AppServerError(f"Codex App Server request {request_id} timed out") from error


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Stop a running child process

    Args:
        process: Child process to terminate

    Raises:
        OSError: If process termination fails
    """
    if process.returncode is not None:
        return

    process.terminate()
    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=SHUTDOWN_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    except TimeoutError:
        process.kill()
        await process.wait()


def read_process_stderr(stderr_file: BinaryIO) -> Optional[str]:
    """Read a bounded and safe App Server stderr tail

    Args:
        stderr_file: Temporary binary file used as process stderr

    Returns:
        Safe diagnostic text or None when stderr is empty
    """
    stderr_file.flush()
    stderr_file.seek(0, os.SEEK_END)
    stderr_size: int = stderr_file.tell()
    stderr_start: int = max(0, stderr_size - MAX_STDERR_BYTES)
    stderr_file.seek(stderr_start)
    stderr_bytes: bytes = stderr_file.read(MAX_STDERR_BYTES)
    stderr_text: str = stderr_bytes.decode(
        "utf-8",
        errors="replace",
    ).strip()
    if not stderr_text:
        return None

    stderr_lower: str = stderr_text.lower()
    for marker in SENSITIVE_STDERR_MARKERS:
        if marker in stderr_lower:
            return REDACTED_STDERR_TEXT
    return stderr_text


async def read_account_limits(account: AccountConfig) -> Dict[str, object]:
    """Fetch the raw rate-limit result for one account

    Args:
        account: Account to query

    Returns:
        Raw result from account/rateLimits/read

    Raises:
        AppServerError: If credentials, process, transport, or response fail
    """
    auth_path: Path = account.codex_home / AUTH_FILE_NAME
    if not auth_path.is_file():
        message: str = (
            f"Account is not logged in; run --login {account.slug} "
            "with the same --config path"
        )
        raise AppServerError(message)

    environment: Dict[str, str] = build_codex_environment(account)
    stderr_file: BinaryIO = tempfile.TemporaryFile(mode="w+b")
    with stderr_file:
        try:
            process: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
                get_codex_command(),
                APP_SERVER_COMMAND,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_file,
                env=environment,
            )
        except OSError as error:
            raise AppServerError(f"Cannot start Codex App Server: {account.slug}") from error

        try:
            try:
                if process.stdin is None:
                    raise AppServerError("Codex App Server stdin is unavailable")
                if process.stdout is None:
                    raise AppServerError("Codex App Server stdout is unavailable")

                initialize_request: Dict[str, object] = build_initialize_request()
                await write_rpc_message(
                    process.stdin,
                    initialize_request,
                )
                await read_rpc_result(
                    process.stdout,
                    INITIALIZE_REQUEST_ID,
                )

                initialized_notification: Dict[str, object] = build_initialized_notification()
                await write_rpc_message(
                    process.stdin,
                    initialized_notification,
                )
                rate_limits_request: Dict[str, object] = build_rate_limits_request()
                await write_rpc_message(
                    process.stdin,
                    rate_limits_request,
                )
                return await read_rpc_result(
                    process.stdout,
                    RATE_LIMITS_REQUEST_ID,
                )
            except OSError as error:
                raise AppServerError(
                    f"Codex App Server transport failed: {account.slug}",
                ) from error
        except asyncio.CancelledError:
            raise
        except AppServerError as error:
            await stop_process(process)
            diagnostic: Optional[str] = read_process_stderr(stderr_file)
            if diagnostic is not None:
                error.add_note(f"{STDERR_NOTE_PREFIX}: {diagnostic}")
            raise
        finally:
            await stop_process(process)


def select_rate_limit_bucket(result: Dict[str, object]) -> Dict[str, object]:
    """Select the canonical Codex rate-limit bucket

    Args:
        result: account/rateLimits/read result

    Returns:
        Canonical Codex bucket

    Raises:
        ProtocolError: If no supported bucket exists
    """
    buckets_value: object = result.get(RATE_LIMITS_BY_ID_FIELD)
    if isinstance(buckets_value, dict):
        codex_value: object = buckets_value.get(CODEX_LIMIT_ID)
        if isinstance(codex_value, dict):
            return codex_value

    fallback_value: object = result.get(RATE_LIMITS_FIELD)
    if isinstance(fallback_value, dict):
        return fallback_value

    raise ProtocolError("Codex rate-limit bucket is missing")


def parse_limit_window(
    value: object,
    field_name: str,
) -> Optional[LimitWindow]:
    """Parse one optional Codex limit window

    Args:
        value: External primary or secondary JSON value
        field_name: Protocol field used in safe error messages

    Returns:
        Validated window or None when the server returned null

    Raises:
        ProtocolError: If the returned window is malformed
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProtocolError(f"{field_name} limit window must be an object")

    used_value: object = value.get(USED_PERCENT_FIELD)
    if isinstance(used_value, bool):
        raise ProtocolError(f"{field_name} usedPercent must be a number")
    try:
        used_percent: float = float(used_value)
    except (TypeError, ValueError, OverflowError) as error:
        error.args = (f"Invalid {field_name} usedPercent value",)
        raise ProtocolError(f"{field_name} usedPercent must be a number") from error

    used_percent_is_valid: bool = math.isfinite(used_percent)
    used_percent_is_valid = used_percent_is_valid and MIN_PERCENT <= used_percent
    used_percent_is_valid = used_percent_is_valid and used_percent <= MAX_PERCENT
    if not used_percent_is_valid:
        raise ProtocolError(f"{field_name} usedPercent is outside 0..100")

    duration_value: object = value.get(WINDOW_DURATION_FIELD)
    if isinstance(duration_value, bool):
        raise ProtocolError(f"{field_name} windowDurationMins must be a positive integer")
    try:
        duration_number: float = float(duration_value)
    except (TypeError, ValueError, OverflowError) as error:
        error.args = (f"Invalid {field_name} windowDurationMins value",)
        raise ProtocolError(
            f"{field_name} windowDurationMins must be a positive integer",
        ) from error

    duration_is_valid: bool = math.isfinite(duration_number)
    duration_is_valid = duration_is_valid and duration_number > 0
    duration_is_valid = duration_is_valid and duration_number.is_integer()
    if not duration_is_valid:
        raise ProtocolError(f"{field_name} windowDurationMins must be a positive integer")
    duration_minutes: int = int(duration_number)

    reset_value: object = value.get(RESETS_AT_FIELD)
    resets_at: Optional[datetime] = None
    if reset_value is not None:
        if isinstance(reset_value, bool):
            raise ProtocolError(f"{field_name} resetsAt must be a Unix timestamp")
        if not isinstance(reset_value, (int, float)):
            raise ProtocolError(f"{field_name} resetsAt must be a Unix timestamp")
        try:
            reset_number: float = float(reset_value)
        except OverflowError as error:
            raise ProtocolError(f"{field_name} resetsAt is outside the supported range") from error
        if not math.isfinite(reset_number):
            raise ProtocolError(f"{field_name} resetsAt must be a Unix timestamp")
        try:
            resets_at = datetime.fromtimestamp(
                reset_number,
                tz=timezone.utc,
            ).astimezone()
        except (OSError, OverflowError, ValueError) as error:
            raise ProtocolError(f"{field_name} resetsAt is outside the supported range") from error

    return LimitWindow(
        duration_minutes=duration_minutes,
        used_percent=used_percent,
        resets_at=resets_at,
    )


def parse_limit_windows(result: Dict[str, object]) -> Dict[int, LimitWindow]:
    """Parse primary and secondary Codex limit windows

    Args:
        result: account/rateLimits/read result

    Returns:
        Valid windows keyed by duration in minutes

    Raises:
        ProtocolError: If a returned window is malformed
    """
    bucket: Dict[str, object] = select_rate_limit_bucket(result)
    windows: Dict[int, LimitWindow] = {}

    for field_name in (PRIMARY_FIELD, SECONDARY_FIELD):
        window: Optional[LimitWindow] = parse_limit_window(
            bucket.get(field_name),
            field_name,
        )
        if window is None:
            continue
        if window.duration_minutes in windows:
            raise ProtocolError("Codex returned duplicate limit durations")
        windows[window.duration_minutes] = window

    return windows


async def get_account_status(account: AccountConfig) -> AccountStatus:
    """Build display status for one account

    Args:
        account: Account to query

    Returns:
        Successful or failed display status
    """
    try:
        result: Dict[str, object] = await read_account_limits(account)
        windows: Dict[int, LimitWindow] = parse_limit_windows(result)
        return AccountStatus(
            account=account,
            windows=windows,
            error_message=None,
        )
    except asyncio.CancelledError:
        raise
    except LimitsError as error:
        logger.exception(
            "Failed to fetch limits: account_slug=%s",
            account.slug,
        )
        return AccountStatus(
            account=account,
            windows={},
            error_message=str(error),
        )
    except Exception:
        logger.exception(
            "Unexpected limit error: account_slug=%s",
            account.slug,
        )
        return AccountStatus(
            account=account,
            windows={},
            error_message="Unexpected account error; see app.log",
        )


def format_percentage(value: float) -> str:
    """Format a percentage with at most one decimal place

    Args:
        value: Percentage to format

    Returns:
        Compact percentage without a redundant decimal zero
    """
    rounded_value: float = round(value, 1)
    if rounded_value.is_integer():
        return str(int(rounded_value))
    return f"{rounded_value:.1f}"


def format_datetime(value: datetime) -> str:
    """Format a timezone-aware datetime for the terminal

    Args:
        value: Datetime to render

    Returns:
        Datetime converted to the local timezone
    """
    local_value: datetime = value.astimezone()
    return local_value.strftime(DATETIME_FORMAT)


def format_refresh_interval(refresh_seconds: int) -> str:
    """Format a configured refresh interval

    Args:
        refresh_seconds: Positive refresh delay in seconds

    Returns:
        Whole minutes when possible, otherwise seconds
    """
    if refresh_seconds % SECONDS_PER_MINUTE == 0:
        refresh_minutes: int = refresh_seconds // SECONDS_PER_MINUTE
        return f"{refresh_minutes} мин"
    return f"{refresh_seconds} с"


def format_limit_line(
    label: str,
    window: Optional[LimitWindow],
) -> str:
    """Format one rate-limit line

    Args:
        label: User-facing window label
        window: Parsed window or None

    Returns:
        Fully formatted indented line
    """
    if window is None:
        return f"  {label}: {NO_DATA_TEXT}"

    remaining_percent: float = MAX_PERCENT - window.used_percent
    percentage_text: str = format_percentage(remaining_percent)
    bar_filled_count: int = round(remaining_percent * PROGRESS_BAR_WIDTH / MAX_PERCENT)
    bar_empty_count: int = PROGRESS_BAR_WIDTH - bar_filled_count
    bar_text: str = PROGRESS_FILLED_CHAR * bar_filled_count + PROGRESS_EMPTY_CHAR * bar_empty_count
    if window.resets_at is None:
        reset_text: str = UNKNOWN_RESET_TEXT
    else:
        reset_datetime: str = format_datetime(window.resets_at)
        reset_text = f"сброс {reset_datetime}"

    return f"  {label}: [{bar_text}] {percentage_text}% осталось, {reset_text}"


def render_screen(
    statuses: Sequence[AccountStatus],
    refreshed_at: datetime,
    refresh_seconds: int,
) -> str:
    """Render a complete monitor snapshot

    Args:
        statuses: Account states in configured order
        refreshed_at: Completion time of the current refresh
        refresh_seconds: Delay before the next refresh

    Returns:
        Complete terminal text without a trailing newline
    """
    refreshed_text: str = format_datetime(refreshed_at)
    interval_text: str = format_refresh_interval(refresh_seconds)
    lines: List[str] = [
        SCREEN_TITLE,
        f"Обновлено: {refreshed_text}",
        f"Следующее обновление: примерно через {interval_text}",
    ]

    for status in statuses:
        lines.append("")
        lines.append(status.account.name)
        if status.error_message is not None:
            lines.append(f"  Ошибка: {status.error_message}")
            continue

        five_hour_window: Optional[LimitWindow] = status.windows.get(FIVE_HOUR_MINUTES)
        week_window: Optional[LimitWindow] = status.windows.get(WEEK_MINUTES)
        lines.append(format_limit_line(FIVE_HOUR_LABEL, five_hour_window))
        lines.append(format_limit_line(WEEK_LABEL, week_window))

    lines.append("")
    lines.append(STOP_TEXT)
    return "\n".join(lines)


def write_screen(screen: str) -> None:
    """Replace the current terminal screen

    Args:
        screen: Fully rendered screen text
    """
    if sys.stdout.isatty():
        sys.stdout.write(CLEAR_SCREEN)
    else:
        sys.stdout.write("\n")

    sys.stdout.write(screen)
    sys.stdout.write("\n")
    sys.stdout.flush()


async def run_monitor(config: AppConfig) -> None:
    """Run the monitor until cancellation

    Args:
        config: Validated application configuration
    """
    while True:
        statuses: List[AccountStatus] = list(
            await asyncio.gather(
                *(get_account_status(account) for account in config.accounts),
            ),
        )
        refreshed_at: datetime = datetime.now().astimezone()
        screen: str = render_screen(
            statuses,
            refreshed_at,
            config.refresh_seconds,
        )
        write_screen(screen)
        await asyncio.sleep(config.refresh_seconds)


def configure_logging() -> None:
    """Configure the protected application error log

    Raises:
        ConfigError: If the log directory or file cannot be prepared
    """
    try:
        STATE_ROOT.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        os.chmod(STATE_ROOT, DIRECTORY_MODE)
        LOG_PATH.touch(exist_ok=True)
        os.chmod(LOG_PATH, FILE_MODE)
        handler: logging.FileHandler = logging.FileHandler(
            LOG_PATH,
            encoding="utf-8",
        )
    except OSError as error:
        raise ConfigError("Cannot prepare application log") from error

    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    logger.propagate = False


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments

    Returns:
        Parsed CLI namespace
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=CLI_DESCRIPTION,
        allow_abbrev=False,
    )
    parser.add_argument(
        CONFIG_OPTION,
        type=Path,
        default=DEFAULT_CONFIG_PATH,
    )
    parser.add_argument(
        LOGIN_OPTION,
        metavar="SLUG",
    )
    return parser.parse_args()


def ensure_codex_available() -> None:
    """Ensure that the required Codex CLI is available

    Raises:
        ConfigError: If Codex CLI cannot be found in PATH
    """
    codex_path: Optional[str] = shutil.which(CODEX_COMMAND)
    if codex_path is None:
        raise ConfigError("Codex CLI is not available in PATH")


def get_codex_command() -> str:
    """Get the platform-compatible Codex command

    Returns:
        Resolved Windows launcher or the portable command name
    """
    if sys.platform != WINDOWS_PLATFORM:
        return CODEX_COMMAND

    codex_path: Optional[str] = shutil.which(CODEX_COMMAND)
    if codex_path is None:
        return CODEX_COMMAND
    return codex_path


def get_account_by_slug(
    accounts: Sequence[AccountConfig],
    slug: str,
) -> AccountConfig:
    """Find a configured account by slug

    Args:
        accounts: Validated account configurations
        slug: Requested account slug

    Returns:
        Matching account configuration

    Raises:
        ConfigError: If the slug is not configured
    """
    for account in accounts:
        if account.slug == slug:
            return account
    raise ConfigError(f"Unknown account slug: {slug}")


def run_application(arguments: argparse.Namespace) -> None:
    """Run login or monitoring according to parsed arguments

    Args:
        arguments: Parsed CLI namespace
    """
    config_path: Path = arguments.config
    login_slug: Optional[str] = arguments.login
    config: AppConfig = load_config(config_path)
    ensure_codex_available()

    if login_slug is not None:
        account: AccountConfig = get_account_by_slug(
            config.accounts,
            login_slug,
        )
        asyncio.run(login_account(account))
        return

    for account in config.accounts:
        prepare_account_home(account)
    asyncio.run(run_monitor(config))


def main() -> int:
    """Run the command-line application

    Returns:
        Process exit code
    """
    arguments: argparse.Namespace = parse_arguments()
    try:
        configure_logging()
    except LimitsError as error:
        sys.stderr.write(f"Error: {error}\n")
        return ERROR_EXIT_CODE

    try:
        run_application(arguments)
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        return INTERRUPTED_EXIT_CODE
    except LimitsError as error:
        logger.exception("Application failed")
        sys.stderr.write(f"Error: {error}\n")
        return ERROR_EXIT_CODE
    except Exception:
        logger.exception("Unexpected application failure")
        sys.stderr.write("Error: Unexpected application failure; see app.log\n")
        return ERROR_EXIT_CODE
    return SUCCESS_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
