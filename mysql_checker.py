#!/usr/bin/env python3
"""
MySQL runtime parameter checker.

Connects to a remote MySQL instance via PyMySQL and retrieves SHOW GLOBAL
VARIABLES, then compares the running parameters with the file-based
configuration.

Credential loading priority (for --mysql-config):
  1. Explicit user/password command-line args
  2. --mysql-config file (MySQL-style config file with [client] section)
  3. ~/.my.cnf
  4. /etc/my.cnf
  5. MYSQL_USER / MYSQL_PASSWORD environment variables

Key features:
- Uses PyMySQL for robust MySQL connectivity
- Data-driven parameter type/mapping model (PARAM_SPEC) — one source of truth
- Maps my.cnf parameter names to MySQL runtime variable names (with `-`/`_`
  equivalence handled at the parser layer)
- Correctly classifies boolean / size / integer / string / path parameters
- Detects discrepancies between file config and running config
- Secure credential loading (no passwords in process list)
- Optional SSL/TLS, parallel connections, best-practice checks
"""

import os
import re
import typing
from pathlib import Path
from collections import OrderedDict


# ---------------------------------------------------------------------------
# Parameter type model (data-driven, single source of truth)
# ---------------------------------------------------------------------------

# Parameter types. Each known my.cnf key is declared once here with its MySQL
# runtime variable name and its semantic type. This replaces the previously
# duplicated boolean list (which also misclassified several string/integer
# parameters).
BOOLEAN = "boolean"      # ON/OFF, accepts on/off/1/0/yes/no/true/false
SIZE = "size"            # byte quantity with optional K/M/G/T suffix
INTEGER = "integer"     # plain integer, no unit
STRING = "string"       # enum / free text, compared case-insensitively
PATH = "path"           # filesystem path, trailing slash-insensitive

# Read-only server variables that can never appear in my.cnf as settable
# options. Comparing them is meaningless, so they are excluded from the spec.
# (Previously the map included version / innodb_version / hostname — removed.)
_READONLY_VARS = {"version", "innodb_version", "hostname"}

# my.cnf key (underscore form) -> (mysql_variable_name, type)
# For the vast majority of options the cnf key equals the variable name; the
# mysql_variable_name is explicit only where they differ (e.g. dashes in the
# cnf form such as pid-file -> pid_file).
PARAM_SPEC: "dict[str, tuple[str, str]]" = {
    # --- Boolean-like ---
    "autocommit": ("autocommit", BOOLEAN),
    "unique_checks": ("unique_checks", BOOLEAN),
    "foreign_key_checks": ("foreign_key_checks", BOOLEAN),
    "sql_log_bin": ("sql_log_bin", BOOLEAN),
    "sql_log_off": ("sql_log_off", BOOLEAN),
    "log_bin": ("log_bin", BOOLEAN),
    "log_slave_updates": ("log_slave_updates", BOOLEAN),
    "relay_log_recovery": ("relay_log_recovery", BOOLEAN),
    "relay_log_purge": ("relay_log_purge", BOOLEAN),
    "skip_name_resolve": ("skip_name_resolve", BOOLEAN),
    "skip_grant_tables": ("skip_grant_tables", BOOLEAN),
    "skip_networking": ("skip_networking", BOOLEAN),
    "skip_show_database": ("skip_show_database", BOOLEAN),
    "skip_external_locking": ("skip_external_locking", BOOLEAN),
    "skip_symbolic_links": ("skip_symbolic_links", BOOLEAN),
    "local_infile": ("local_infile", BOOLEAN),
    "old_passwords": ("old_passwords", BOOLEAN),
    "explicit_defaults_for_timestamp": ("explicit_defaults_for_timestamp", BOOLEAN),
    "performance_schema": ("performance_schema", BOOLEAN),
    "symbolic_links": ("symbolic_links", BOOLEAN),
    "log_slow_queries": ("log_slow_queries", BOOLEAN),
    "slow_query_log": ("slow_query_log", BOOLEAN),
    "general_log": ("general_log", BOOLEAN),
    "log_queries_not_using_indexes": ("log_queries_not_using_indexes", BOOLEAN),
    "log_slow_slave_statements": ("log_slow_slave_statements", BOOLEAN),
    "log_slow_admin_statements": ("log_slow_admin_statements", BOOLEAN),
    "log_bin_trust_function_creators": ("log_bin_trust_function_creators", BOOLEAN),
    "enforce_gtid_consistency": ("enforce_gtid_consistency", BOOLEAN),
    "gtid_mode": ("gtid_mode", STRING),  # OFF/ON/ON_PERMISSIVE/...
    "read_only": ("read_only", BOOLEAN),
    "super_read_only": ("super_read_only", BOOLEAN),
    "slave_preserve_commit_order": ("slave_preserve_commit_order", BOOLEAN),

    # --- Size (bytes, with K/M/G/T suffix) ---
    "innodb_buffer_pool_size": ("innodb_buffer_pool_size", SIZE),
    "innodb_log_file_size": ("innodb_log_file_size", SIZE),
    "innodb_log_buffer_size": ("innodb_log_buffer_size", SIZE),
    "innodb_redo_log_capacity": ("innodb_redo_log_capacity", SIZE),
    "max_allowed_packet": ("max_allowed_packet", SIZE),
    "sort_buffer_size": ("sort_buffer_size", SIZE),
    "read_buffer_size": ("read_buffer_size", SIZE),
    "read_rnd_buffer_size": ("read_rnd_buffer_size", SIZE),
    "join_buffer_size": ("join_buffer_size", SIZE),
    "binlog_cache_size": ("binlog_cache_size", SIZE),
    "binlog_stmt_cache_size": ("binlog_stmt_cache_size", SIZE),
    "tmp_table_size": ("tmp_table_size", SIZE),
    "max_heap_table_size": ("max_heap_table_size", SIZE),
    "max_relay_log_size": ("max_relay_log_size", SIZE),
    "thread_stack": ("thread_stack", SIZE),
    "key_buffer_size": ("key_buffer_size", SIZE),
    "myisam_sort_buffer_size": ("myisam_sort_buffer_size", SIZE),
    "innodb_log_files_in_group": ("innodb_log_files_in_group", INTEGER),
    "innodb_data_file_path": ("innodb_data_file_path", STRING),
    "innodb_doublewrite": ("innodb_doublewrite", BOOLEAN),

    # --- Integer (plain, no unit) ---
    "max_connections": ("max_connections", INTEGER),
    "max_user_connections": ("max_user_connections", INTEGER),
    "max_connect_errors": ("max_connect_errors", INTEGER),
    "wait_timeout": ("wait_timeout", INTEGER),
    "interactive_timeout": ("interactive_timeout", INTEGER),
    "net_read_timeout": ("net_read_timeout", INTEGER),
    "net_write_timeout": ("net_write_timeout", INTEGER),
    "connect_timeout": ("connect_timeout", INTEGER),
    "innodb_flush_log_at_trx_commit": ("innodb_flush_log_at_trx_commit", INTEGER),
    "innodb_io_capacity": ("innodb_io_capacity", INTEGER),
    "innodb_io_capacity_max": ("innodb_io_capacity_max", INTEGER),
    "innodb_read_io_threads": ("innodb_read_io_threads", INTEGER),
    "innodb_write_io_threads": ("innodb_write_io_threads", INTEGER),
    "innodb_buffer_pool_instances": ("innodb_buffer_pool_instances", INTEGER),
    "innodb_open_files": ("innodb_open_files", INTEGER),
    "innodb_print_all_deadlocks": ("innodb_print_all_deadlocks", BOOLEAN),
    "table_open_cache": ("table_open_cache", INTEGER),
    "table_definition_cache": ("table_definition_cache", INTEGER),
    "thread_cache_size": ("thread_cache_size", INTEGER),
    "max_prepared_stmt_count": ("max_prepared_stmt_count", INTEGER),
    "open_files_limit": ("open_files_limit", INTEGER),
    "lower_case_table_names": ("lower_case_table_names", INTEGER),
    "slave_parallel_workers": ("slave_parallel_workers", INTEGER),
    "slave_preserve_commit_order_workers": ("slave_preserve_commit_order_workers", INTEGER),
    "sync_binlog": ("sync_binlog", INTEGER),
    "sync_relay_log": ("sync_relay_log", INTEGER),
    "sync_relay_log_info": ("sync_relay_log_info", INTEGER),
    "sync_master_info": ("sync_master_info", INTEGER),
    "port": ("port", INTEGER),
    "max_binlog_size": ("max_binlog_size", SIZE),
    "max_binlog_cache_size": ("max_binlog_cache_size", SIZE),
    "binlog_expire_logs_seconds": ("binlog_expire_logs_seconds", INTEGER),
    "expire_logs_days": ("expire_logs_days", INTEGER),
    "auto_increment_increment": ("auto_increment_increment", INTEGER),
    "auto_increment_offset": ("auto_increment_offset", INTEGER),
    "long_query_time": ("long_query_time", STRING),  # may be float, compare as text

    # --- String / enum ---
    "innodb_flush_method": ("innodb_flush_method", STRING),
    "binlog_format": ("binlog_format", STRING),
    "binlog_row_image": ("binlog_row_image", STRING),
    "binlog_rows_query_log_events": ("binlog_rows_query_log_events", BOOLEAN),
    "binlog_checksum": ("binlog_checksum", STRING),
    "slave_sql_verify_checksum": ("slave_sql_verify_checksum", BOOLEAN),
    "slave_parallel_type": ("slave_parallel_type", STRING),
    "relay_log_space_limit": ("relay_log_space_limit", SIZE),
    "relay_log_info_repository": ("relay_log_info_repository", STRING),
    "master_info_repository": ("master_info_repository", STRING),
    "log_replica_updates": ("log_replica_updates", BOOLEAN),
    "replicate_do_db": ("replicate_do_db", STRING),
    "replicate_ignore_db": ("replicate_ignore_db", STRING),
    "character_set_server": ("character_set_server", STRING),
    "collation_server": ("collation_server", STRING),
    "init_connect": ("init_connect", STRING),
    "sql_mode": ("sql_mode", STRING),
    "default_storage_engine": ("default_storage_engine", STRING),
    "default_authentication_plugin": ("default_authentication_plugin", STRING),
    "default_time_zone": ("default_time_zone", STRING),
    "event_scheduler": ("event_scheduler", STRING),

    # --- Path ---
    "socket": ("socket", PATH),
    "basedir": ("basedir", PATH),
    "datadir": ("datadir", PATH),
    "pid_file": ("pid_file", PATH),
    "log_error": ("log_error", PATH),
    "slow_query_log_file": ("slow_query_log_file", PATH),
    "general_log_file": ("general_log_file", PATH),
    "relay_log": ("relay_log", PATH),
    "relay_log_index": ("relay_log_index", PATH),
    "innodb_data_home_dir": ("innodb_data_home_dir", PATH),
    "innodb_log_group_home_dir": ("innodb_log_group_home_dir", PATH),
    "innodb_undo_directory": ("innodb_undo_directory", PATH),
    "secure_file_priv": ("secure_file_priv", PATH),
    "tmpdir": ("tmpdir", PATH),
    "log_bin_basename": ("log_bin_basename", PATH),
    "relay_log_basename": ("relay_log_basename", PATH),
    "log_error_verbosity": ("log_error_verbosity", INTEGER),

    # --- Unique identifiers ---
    "server_id": ("server_id", INTEGER),
    "report_host": ("report_host", STRING),
    "report_password": ("report_password", STRING),
    "report_port": ("report_port", INTEGER),
    "report_user": ("report_user", STRING),
}


def cnf_to_mysql_var_map() -> "dict[str, str]":
    """
    Backward-compatible view of PARAM_SPEC: {cnf_key: mysql_var_name}.

    Kept for callers that only need the name mapping (not the type).
    """
    return {k: v[0] for k, v in PARAM_SPEC.items()}


def _infer_type(value: str) -> str:
    """Infer a parameter type from its value when the key is not in PARAM_SPEC."""
    v = value.strip()
    if re.match(r"^\d+(?:\.\d+)?\s*[KMGTkmgt]?$", v):
        # Has a size unit (or is a bare number that could be size); treat as
        # size so 1G == 1073741824 even for unknown keys.
        return SIZE
    if re.match(r"^-?\d+$", v):
        return INTEGER
    return STRING


def lookup_param(cnf_key: str) -> "tuple[str, str]":
    """
    Resolve a my.cnf key (already dash-normalized to underscore form) to
    (mysql_variable_name, type). Unknown keys fall back to identity mapping
    with a type inferred from the value later via normalize_value.
    """
    key = cnf_key.replace("-", "_")
    spec = PARAM_SPEC.get(key)
    if spec:
        return spec
    # Default: variable name equals the key, type inferred per-value.
    return key, _NULL_TYPE_SENTINEL


# Sentinel meaning "infer type from the value at normalization time".
_NULL_TYPE_SENTINEL = "__infer__"


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _parse_mysql_config_file(
    path: str,
    sections: "tuple[str, ...]" = ("client",),
    user_keys: "tuple[str, ...]" = ("user",),
) -> "tuple[typing.Optional[str], typing.Optional[str], typing.Optional[int], typing.Optional[str]]":
    """
    Parse a MySQL-style config file (e.g. ~/.my.cnf) and return credentials.

    `sections` lists the sections to honor, in ascending priority order: a
    key found in a later section overrides one found in an earlier section.
    Defaults to the `[client]` section and the `user` key (MySQL convention).

    `user_keys` are the key names accepted for the username (so
    `--password-conf` files that use `username=` instead of `user=` work).

    Returns:
        (user, password, port, socket_path)
    """
    section_priority = {s.lower(): i for i, s in enumerate(sections)}
    user_key_set = {k.lower() for k in user_keys}

    # per_section[section] = {user, password, port, socket}
    per_section: "dict[str, dict[str, typing.Any]]" = {}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, IOError):
        return None, None, None, None

    current: typing.Optional[str] = None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue

        section_match = re.match(r"^\[([^\]]+)\]$", stripped)
        if section_match:
            name = section_match.group(1).lower()
            current = name if name in section_priority else None
            if current is not None and current not in per_section:
                per_section[current] = {}
            continue

        if current is None:
            continue

        kv_match = re.match(r"^([^=\s;#]+)\s*=\s*(.*?)\s*$", stripped)
        if not kv_match:
            continue
        key = kv_match.group(1).strip().lower()
        value = kv_match.group(2).strip()

        bucket = per_section[current]
        if key in user_key_set:
            bucket["user"] = value
        elif key == "password":
            bucket["password"] = value
        elif key == "port":
            try:
                bucket["port"] = int(value)
            except ValueError:
                pass
        elif key == "socket":
            bucket["socket"] = value

    # Merge sections in ascending priority order; later overrides earlier.
    user: typing.Optional[str] = None
    password: typing.Optional[str] = None
    port: typing.Optional[int] = None
    socket_path: typing.Optional[str] = None
    for sec in sorted(per_section, key=lambda s: section_priority[s]):
        b = per_section[sec]
        if b.get("user"):
            user = b["user"]
        if b.get("password"):
            password = b["password"]
        if b.get("port") is not None:
            port = b["port"]
        if b.get("socket"):
            socket_path = b["socket"]

    return user, password, port, socket_path


def load_credentials(
    config_file: typing.Optional[str] = None,
    password_conf: typing.Optional[str] = None,
    explicit_user: typing.Optional[str] = None,
    explicit_password: typing.Optional[str] = None,
) -> "tuple[typing.Optional[str], typing.Optional[str], typing.Optional[int], typing.Optional[str]]":
    """
    Load MySQL credentials with the following priority (highest first):

      1. Explicit user/password (programmatic, not exposed on the CLI to
         avoid leaking secrets via the process list)
      2. --password-conf file (MySQL-style config file; honors [mysql]
         section, accepts both `user` and `username` keys)
      3. --mysql-config file (MySQL-style config file with [client] section)
      4. ~/.my.cnf
      5. /etc/my.cnf
      6. MYSQL_USER / MYSQL_PASSWORD environment variables

    Returns:
        (user, password, port, socket_path)
    """
    file_user: typing.Optional[str] = None
    file_password: typing.Optional[str] = None
    file_port: typing.Optional[int] = None
    file_socket: typing.Optional[str] = None

    def _take(creds: tuple) -> None:
        """Fill unfilled fields from `creds` (lower priority than what's set)."""
        nonlocal file_user, file_password, file_port, file_socket
        u, p, po, s = creds
        if not file_user and u:
            file_user = u
        if not file_password and p:
            file_password = p
        if file_port is None and po is not None:
            file_port = po
        if not file_socket and s:
            file_socket = s

    # 2. --password-conf: [mysql] section (with [client] as lower priority),
    # accepting both `user` and `username` keys.
    if password_conf:
        _take(_parse_mysql_config_file(
            password_conf,
            sections=("client", "mysql"),
            user_keys=("user", "username"),
        ))

    # 3. --mysql-config: [client] section only (backward compatible).
    if config_file:
        _take(_parse_mysql_config_file(config_file, sections=("client",)))

    # 4-5. Default config file locations.
    if not file_user:
        for default_path in [os.path.expanduser("~/.my.cnf"), "/etc/my.cnf"]:
            if os.path.exists(default_path):
                fu, fp, fpo, fs = _parse_mysql_config_file(default_path)
                if fu:
                    file_user, file_password, file_port, file_socket = (
                        fu, fp, fpo, fs
                    )
                    break

    # 6. Environment variable fallback
    if not file_user:
        file_user = os.environ.get("MYSQL_USER")
    if not file_password and os.environ.get("MYSQL_PASSWORD"):
        file_password = os.environ.get("MYSQL_PASSWORD")
    if not file_socket and os.environ.get("MYSQL_UNIX_PORT"):
        file_socket = os.environ.get("MYSQL_UNIX_PORT")
    if not file_port and os.environ.get("MYSQL_TCP_PORT"):
        try:
            file_port = int(os.environ.get("MYSQL_TCP_PORT", ""))
        except ValueError:
            pass

    return (
        explicit_user or file_user,
        explicit_password or file_password,
        file_port,
        file_socket,
    )


# ---------------------------------------------------------------------------
# MySQL connection
# ---------------------------------------------------------------------------

DEFAULT_CONNECT_TIMEOUT = 10


def get_running_variables(
    host: str,
    port: int = 3306,
    user: typing.Optional[str] = None,
    password: typing.Optional[str] = None,
    socket_path: typing.Optional[str] = None,
    ssl: typing.Optional[dict] = None,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    retries: int = 0,
) -> "tuple[dict[str, str], typing.Optional[str]]":
    """
    Connect to MySQL via PyMySQL and retrieve SHOW GLOBAL VARIABLES.

    Args:
        host: MySQL server hostname/IP
        port: MySQL server port (default 3306)
        user: MySQL username
        password: MySQL password
        socket_path: Unix socket path (alternative to host:port)
        ssl: Optional SSL dict forwarded to PyMySQL (keys: ca/cert/key/...)
        connect_timeout: TCP connect timeout in seconds
        retries: Number of additional connection attempts on transient failure

    Returns:
        tuple: (variables_dict, error_message)
            variables_dict: {variable_name: value}
            error_message: Error description if connection failed, None otherwise
    """
    try:
        import pymysql
    except ImportError:
        return {}, "PyMySQL 未安装，请执行: pip3 install PyMySQL"

    connect_kwargs: typing.Dict[str, typing.Any] = {}
    if socket_path:
        connect_kwargs["unix_socket"] = socket_path
    else:
        connect_kwargs["host"] = host
        connect_kwargs["port"] = port
    if user:
        connect_kwargs["user"] = user
    if password:
        connect_kwargs["password"] = password
    connect_kwargs["charset"] = "utf8mb4"
    connect_kwargs["use_unicode"] = True
    connect_kwargs["connect_timeout"] = connect_timeout
    if ssl:
        connect_kwargs["ssl"] = ssl

    last_err: typing.Optional[str] = None
    attempts = retries + 1
    for _ in range(attempts):
        try:
            conn = pymysql.connect(**connect_kwargs)
            cursor = conn.cursor()
            cursor.execute("SHOW GLOBAL VARIABLES")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()

            variables: "dict[str, str]" = OrderedDict()
            for row in rows:
                var_name = str(row[0])
                var_value = str(row[1])
                variables[var_name] = var_value
            return variables, None
        except pymysql.err.OperationalError as e:
            last_err = (
                f"连接 {host}:{port} 失败: "
                f"{e.args[1] if len(e.args) > 1 else str(e)}"
            )
        except Exception as e:  # noqa: BLE001 - report any connection failure
            last_err = f"连接 {host}:{port} 异常: {str(e)}"
        # On failure, loop to retry (if retries > 0).

    return {}, last_err or "连接失败（未知原因）"


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------

def normalize_boolean(value: str) -> str:
    """
    Normalize boolean values to ON/OFF.

    Accepts: on/off, 1/0, yes/no, true/false, and empty (treated as OFF).
    """
    v = value.strip().lower()
    if v in ("on", "1", "yes", "true"):
        return "ON"
    if v in ("off", "0", "no", "false", ""):
        return "OFF"
    return value


def normalize_numeric(value: str) -> str:
    """
    Normalize numeric size values to bytes.

    Accepts a number with optional K/M/G/T suffix (case-insensitive) and
    returns the equivalent byte count as a string. Non-matching input is
    returned unchanged.
    """
    value = value.strip()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGTkmgt])?$", value)
    if match:
        num = float(match.group(1))
        unit = (match.group(2) or "").upper()

        multipliers = {
            "": 1,
            "K": 1024,
            "M": 1024 ** 2,
            "G": 1024 ** 3,
            "T": 1024 ** 4,
        }

        normalized = int(num * multipliers.get(unit, 1))
        return str(normalized)

    return value


def normalize_integer(value: str) -> str:
    """Normalize an integer value to a canonical integer string."""
    v = value.strip()
    match = re.match(r"^-?\d+$", v)
    if match:
        return str(int(v))
    # Fall back to size normalization (handles values like '1G' which are
    # technically not plain integers but should still compare consistently).
    return normalize_numeric(v)


def normalize_path(value: str) -> str:
    """Normalize a filesystem path: strip surrounding quotes and trailing slash."""
    v = value.strip().strip('"').strip("'")
    # Collapse a single trailing slash (keep root '/' intact).
    if len(v) > 1 and v.endswith("/"):
        v = v.rstrip("/")
    return v


def normalize_value(cnf_key: str, value: str) -> str:
    """
    Normalize a config value to its canonical comparison form, using the
    parameter's declared type from PARAM_SPEC (or an inferred type for
    unknown keys).

    Args:
        cnf_key: my.cnf key in underscore form (caller normalizes dashes)
        value: raw value string

    Returns:
        Canonical string for equality comparison.
    """
    var_name, ptype = lookup_param(cnf_key)

    if ptype is _NULL_TYPE_SENTINEL:
        ptype = _infer_type(value)

    if ptype == BOOLEAN:
        return normalize_boolean(value)
    if ptype == SIZE:
        return normalize_numeric(value)
    if ptype == INTEGER:
        return normalize_integer(value)
    if ptype == PATH:
        return normalize_path(value)
    # STRING: case-insensitive comparison (MySQL returns uppercase for enums,
    # while my.cnf authors often write lowercase).
    return value.strip().lower()


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_file_vs_running(
    file_config: "dict[str, str]",
    running_vars: "dict[str, str]",
) -> "list[str]":
    """
    Compare file-based configuration with running parameters.

    For each parameter declared in the file config, look up its MySQL
    runtime variable name and type (via PARAM_SPEC / lookup_param), normalize
    both sides, and report any discrepancy.

    Args:
        file_config: Parameters from my.cnf file (keys in underscore form)
        running_vars: Parameters from SHOW GLOBAL VARIABLES

    Returns:
        list[str]: List of discrepancy descriptions
    """
    discrepancies: list[str] = []

    for cnf_key, cnf_value in file_config.items():
        var_name, _ptype = lookup_param(cnf_key)

        if var_name in _READONLY_VARS:
            continue

        running_value = running_vars.get(var_name)

        if running_value is None:
            discrepancies.append(
                f"文件配置中存在但运行参数中缺失: [{cnf_key}] = {cnf_value}"
            )
            continue

        cnf_normalized = normalize_value(cnf_key, cnf_value)
        running_normalized = normalize_value(cnf_key, running_value)

        if cnf_normalized != running_normalized:
            discrepancies.append(
                f"文件配置 [{cnf_key}] = {cnf_value} "
                f"但运行参数 [{var_name}] = {running_value}"
            )

    return discrepancies


# ---------------------------------------------------------------------------
# Best-practice checks
# ---------------------------------------------------------------------------

# Each rule: (variable_name, expected-canonical-value-or-predicate, severity, message)
# A predicate is a callable taking the raw value str -> bool (True=ok).

def _is_on(v: str) -> bool:
    return normalize_boolean(v) == "ON"


def _is_int_in(lo: int, hi: int):
    def _check(v: str) -> bool:
        try:
            return lo <= int(str(v).strip()) <= hi
        except ValueError:
            return False
    return _check


def _is_off(v: str) -> bool:
    return normalize_boolean(v) == "OFF"


def check_best_practices(
    running_vars: "dict[str, str]",
) -> "list[tuple[str, str, str]]":
    """
    Run a set of best-practice / hardening checks against running variables.

    Returns:
        list of (severity, variable_name, message) where severity is one of
        "WARN" / "INFO". Critical findings use "WARN".
    """
    findings: "list[tuple[str, str, str]]" = []

    def get(name: str) -> typing.Optional[str]:
        v = running_vars.get(name)
        return v if v is not None else None

    # --- Replication & durability ---
    v = get("innodb_flush_log_at_trx_commit")
    if v is not None:
        if str(v).strip() != "1":
            findings.append((
                "WARN", "innodb_flush_log_at_trx_commit",
                f"建议为 1 以保证事务持久性（崩溃不丢已提交事务），当前 = {v}",
            ))

    v = get("sync_binlog")
    if v is not None:
        if str(v).strip() == "0":
            findings.append((
                "WARN", "sync_binlog",
                "建议非 0（如 1）以避免主机崩溃丢失 binlog 事件，当前 = 0",
            ))

    v = get("binlog_format")
    if v is not None:
        if str(v).strip().upper() not in ("ROW",):
            findings.append((
                "WARN", "binlog_format",
                f"复制场景建议 ROW（最安全），当前 = {v}",
            ))

    v = get("log_bin")
    if v is not None:
        if not _is_on(v):
            findings.append((
                "INFO", "log_bin",
                "未开启 binlog，无法做时间点恢复与复制",
            ))

    v = get("gtid_mode")
    if v is not None:
        if str(v).strip().upper() in ("OFF", "OFF_PERMISSIVE"):
            findings.append((
                "INFO", "gtid_mode",
                f"未启用 GTID（建议 ON_PERMISSIVE→ON），当前 = {v}",
            ))

    # --- Security / hardening ---
    v = get("skip_name_resolve")
    if v is not None:
        if not _is_on(v):
            findings.append((
                "WARN", "skip_name_resolve",
                "建议开启以禁用主机名解析（避免 DNS 抖动与连接卡住），当前关闭",
            ))

    v = get("local_infile")
    if v is not None:
        if _is_on(v):
            findings.append((
                "WARN", "local_infile",
                "建议关闭以禁止 LOAD DATA LOCAL INFILE（攻击面），当前开启",
            ))

    v = get("secure_file_priv")
    if v is not None:
        if str(v).strip() == "":
            findings.append((
                "INFO", "secure_file_priv",
                "secure_file_priv 为空，LOAD_FILE/导出不受限路径约束",
            ))

    v = get("sql_mode")
    if v is not None:
        s = str(v).strip()
        if s == "" or "NO_ENGINE_SUBSTITUTION" not in s.upper():
            # only a soft hint
            findings.append((
                "INFO", "sql_mode",
                f"sql_mode 未包含严格模式集合，建议含 STRICT_TRANS_TABLES 等，当前 = {v}",
            ))

    # --- Capacity sanity ---
    v = get("max_connections")
    if v is not None:
        if not _is_int_in(50, 100000)(v):
            findings.append((
                "WARN", "max_connections",
                f"max_connections={v} 异常（建议 50~10000 区间复核）",
            ))

    v = get("innodb_buffer_pool_size")
    if v is not None:
        # Just report the value in human form; can't know RAM here, so INFO.
        try:
            bytes_val = int(normalize_numeric(str(v)))
            findings.append((
                "INFO", "innodb_buffer_pool_size",
                f"innodb_buffer_pool_size={bytes_val} 字节（"
                f"{bytes_val / (1024 ** 3):.2f} GB），请确认占物理内存 50%~75%",
            ))
        except (ValueError, TypeError):
            findings.append((
                "INFO", "innodb_buffer_pool_size",
                f"innodb_buffer_pool_size={v}（无法解析为字节数）",
            ))

    v = get("max_connect_errors")
    if v is not None:
        try:
            if int(str(v).strip()) < 100:
                findings.append((
                    "WARN", "max_connect_errors",
                    f"max_connect_errors={v} 偏小，易因网络抖动触发主机被封",
                ))
        except ValueError:
            pass

    return findings
