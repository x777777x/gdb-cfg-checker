#!/usr/bin/env python3
"""
Compare my.cnf files across multiple IP directories, and optionally compare
each file's configuration against the running MySQL parameters of the
instance identified by the IP embedded in the file path.

Expected directory layout (default recursive search):
    <base_dir>/<ip1>/.../my.cnf
    <base_dir>/<ip2>/.../my.cnf

Usage:
    # Basic file-to-file comparison
    python3 compare_my_cnf.py <base_directory> --mode file

    # Configuration + running-parameter comparison
    python3 compare_my_cnf.py <base_directory> \\
        --mode file-vs-running --mysql-config <config_file> [--mysql-port <port>]

    # Custom path pattern (with {ip} placeholder)
    python3 compare_my_cnf.py <base_directory> \\
        --config-path "{ip}/data/goldendb/nudb1/etc/my.cnf"

    # Allow expected differences (e.g. server_id must differ per host)
    python3 compare_my_cnf.py <base_directory> --allow-diff "server_id,port"

    # Parallel connections + JSON output + strict exit code
    python3 compare_my_cnf.py <base_directory> --mode file-vs-running \\
        --mysql-config ~/.my.cnf --workers 8 --json --strict
"""

import argparse
import json
import os
import re
import sys
import typing
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Import the MySQL runtime checker module
from mysql_checker import (
    get_running_variables,
    compare_file_vs_running,
    load_credentials,
    normalize_value,
    check_best_practices,
)


# ---------------------------------------------------------------------------
# Path / IP helpers (deduplicated; previously extract_ip & extract_ip_simple
# were near-duplicate functions).
# ---------------------------------------------------------------------------

_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def extract_ip(path: Path) -> str:
    """
    Extract a host identifier from a my.cnf path.

    Strategy (in order):
      1. The first path segment that looks like an IPv4 address — the most
         reliable signal for GoldenDB's per-host layout. This is preferred
         over the 'data' heuristic so that a base directory whose own path
         contains a 'data' segment (e.g. /data/goldendb/<ip>/...) does not
         mislead extraction.
      2. The segment immediately preceding the LAST 'data' directory in the
         path (the per-host data dir sits near the filename: .../<ip>/data/...).
      3. Fall back to the path's parent directory name.
    """
    parts = str(path).split("/")

    # 1. IPv4 segment.
    for part in parts:
        if _IPV4_RE.match(part):
            return part

    # 2. Segment before the last 'data' directory (case-insensitive for
    # cross-platform paths like /data/… on macOS where the volume is "Data").
    last_data = -1
    for i, part in enumerate(parts):
        if part.lower() == "data":
            last_data = i
    if last_data > 0:
        return parts[last_data - 1]

    # 3. Fallback.
    return path.parent.name


# ---------------------------------------------------------------------------
# my.cnf discovery
# ---------------------------------------------------------------------------

# File names treated as MySQL config. Previously the search required the
# substring "goldendb" in the path, which made the tool unusable for any
# other deployment. The name set is now configurable via --config-name.
DEFAULT_CONFIG_NAMES = ("my.cnf", "mysqld.cnf", "mysql.cnf")


def find_my_cnf_files(
    base_dir: str,
    config_path_pattern: typing.Optional[str] = None,
    config_names: typing.Tuple[str, ...] = DEFAULT_CONFIG_NAMES,
) -> typing.List[Path]:
    """
    Recursively find all MySQL config files matching the expected layout.

    Args:
        base_dir: Base directory to search
        config_path_pattern: Optional path pattern with {ip} placeholder.
            If provided, constructs expected paths instead of searching.
        config_names: File names to match during recursive search.

    Returns:
        list[Path]: Sorted list of config file paths (may be empty)
    """
    cnf_files: list[Path] = []
    base = Path(base_dir)
    if not base.is_dir():
        print(f"错误: 目录不存在: {base_dir}")
        return cnf_files

    if config_path_pattern:
        if "{ip}" not in config_path_pattern:
            print("错误: --config-path 模板必须包含 {ip} 占位符")
            return cnf_files
        for item in base.iterdir():
            if not item.is_dir():
                continue
            ip = extract_ip(item)
            expected_path = config_path_pattern.replace("{ip}", ip)
            full_path = base / expected_path
            if full_path.exists():
                cnf_files.append(full_path)
            else:
                print(f"警告: 配置文件不存在: {full_path}")
    else:
        name_set = set(config_names)
        for path in base.rglob("*"):
            if path.is_file() and path.name in name_set:
                cnf_files.append(path)

    cnf_files.sort()
    return cnf_files


# ---------------------------------------------------------------------------
# my.cnf parsing
# ---------------------------------------------------------------------------

def _strip_inline_comment(line: str) -> str:
    """
    Strip a trailing inline comment.

    MySQL my.cnf officially only treats a leading # or ; as a comment, but
    many hand-edited files use a trailing ` # note` for documentation. To
    avoid corrupting values that legitimately contain '#' (rare, and always
    preceded by a non-space in such cases), we only strip a comment token
    that is preceded by whitespace (or at the start of the line).
    """
    for i, ch in enumerate(line):
        if ch in ("#", ";") and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _is_directive_line(line: str) -> typing.Optional[typing.Tuple[str, str]]:
    """
    Detect a my.cnf preprocessor directive: !include or !includedir.
    Returns (directive, argument) or None.
    """
    stripped = line.strip()
    m = re.match(r"^!(include(?:dir)?)\s+(.+?)\s*$", stripped, re.IGNORECASE)
    if not m:
        return None
    directive = m.group(1).lower()
    argument = m.group(2).strip()
    return directive, argument


def parse_my_cnf(
    content: typing.Optional[str],
    source_label: str = "",
    _seen: typing.Optional[set] = None,
    base_dir: typing.Optional[Path] = None,
) -> "tuple[OrderedDict, list[str]]":
    """
    Parse my.cnf into sections and key-value pairs.

    Handles:
      - section headers [mysqld]
      - key = value pairs (with `-` normalized to `_` in the key)
      - bare boolean options (e.g. `skip-name-resolve`) -> stored as ON
      - inline ` # ...` / ` ; ...` comments
      - !include / !includedir directives (recursive, with cycle detection)

    Returns:
        (sections, duplicates)
        sections:    {section: {key: value, ...}}   (last value wins)
        duplicates:  list of warning strings for duplicate keys within a section
    """
    sections: "OrderedDict[str, OrderedDict[str, str]]" = OrderedDict()
    duplicates: list[str] = []
    current_section: typing.Optional[str] = None

    if _seen is None:
        _seen = set()

    for raw_line in (content or "").splitlines():
        line = _strip_inline_comment(raw_line).strip()

        if not line or line.startswith("#") or line.startswith(";"):
            continue

        # Preprocessor directives: !include / !includedir
        directive = _is_directive_line(line)
        if directive:
            name, argument = directive
            if base_dir is None:
                # No base context to resolve includes from; skip silently.
                continue
            target = Path(argument)
            if not target.is_absolute():
                target = base_dir / target
            _apply_include(
                name, target, sections, duplicates, source_label, _seen
            )
            continue

        # Section header
        section_match = re.match(r"^\[([^\]]+)\]$", line)
        if section_match:
            current_section = section_match.group(1).strip()
            if current_section not in sections:
                sections[current_section] = OrderedDict()
            continue

        # No section yet: file-level directive before any [section]; skip.
        if current_section is None:
            continue

        # key = value
        kv_match = re.match(r"^([^=\s;#]+)\s*=\s*(.*?)\s*$", line)
        if kv_match:
            key = kv_match.group(1).strip().replace("-", "_")
            value = kv_match.group(2).strip()
        else:
            # Bare option with no '=' — only accept it if it looks like a
            # boolean-style option name (alphanumerics, dashes, underscores,
            # dots). MySQL treats such a bare option as enabled (ON).
            if not re.match(r"^[A-Za-z][\w.\-]*$", line):
                continue
            key = line.replace("-", "_")
            value = "ON"

        sec = sections[current_section]
        if key in sec:
            old_val = sec[key]
            duplicates.append(
                f"[{current_section}] 重复 key: '{key}' "
                f"(历史值='{old_val}', 当前值='{value}')"
                f"{' ← 已在 ' + source_label if source_label else ''}"
            )
        sec[key] = value  # last value wins (matches MySQL behavior)

    return sections, duplicates


def _apply_include(
    name: str,
    target: Path,
    sections: "OrderedDict[str, OrderedDict[str, str]]",
    duplicates: typing.List[str],
    source_label: str,
    seen: set,
) -> None:
    """Recursively apply an !include / !includedir directive into `sections`."""
    resolved = target.resolve()
    if resolved in seen:
        duplicates.append(f"检测到 include 环路，跳过: {target}")
        return
    seen.add(resolved)

    targets: list[Path] = []
    if name == "includedir":
        if target.is_dir():
            targets = sorted(p for p in target.iterdir() if p.is_file())
        else:
            return
    else:  # include
        if target.is_file():
            targets = [target]
        else:
            duplicates.append(f"include 目标不存在: {target}")
            return

    for t in targets:
        try:
            sub_content = t.read_text(encoding="utf-8", errors="replace")
        except (OSError, IOError) as e:
            duplicates.append(f"读取 include 文件失败 {t}: {e}")
            continue
        sub_sections, sub_dups = parse_my_cnf(
            sub_content, source_label=str(t), _seen=seen, base_dir=t.parent
        )
        _merge_sections(sections, sub_sections, duplicates)
        for d in sub_dups:
            duplicates.append(f"[{t}] {d}")


def _merge_sections(
    dst: "OrderedDict[str, OrderedDict[str, str]]",
    src: "OrderedDict[str, OrderedDict[str, str]]",
    duplicates: typing.List[str],
) -> None:
    """Merge src sections into dst; later values override earlier ones."""
    for section, kvs in src.items():
        if section not in dst:
            dst[section] = OrderedDict()
        for k, v in kvs.items():
            if k in dst[section]:
                old = dst[section][k]
                duplicates.append(
                    f"[{section}] 重复 key (include 合并): '{k}' "
                    f"(历史值='{old}', 当前值='{v}')"
                )
            dst[section][k] = v


# ---------------------------------------------------------------------------
# Comparison (file vs file)
# ---------------------------------------------------------------------------

def build_cross_compare(
    parsed: "dict",
    ips: "list",
    section_filter: typing.Optional[str] = None,
) -> dict:
    """
    Compare all hosts' parsed configs at once (横向对比).

    For each (section, key) appearing in any host:
      - If every host defines it with the same normalized value -> identical,
        counted and skipped (not reported).
      - Otherwise -> a 'differing' entry: hosts grouped by normalized value
        (each group: first-seen raw value, count, member ips), plus
        missing_hosts for hosts that don't define the key.

    Normalization is by parameter type (via normalize_value), so 1G and
    1073741824, ON and on, log-bin and log_bin are recognized as equal.

    Returns:
        {"identical_count", "differing_count", "differing": [...]}
    """
    # Collect every (section, key) across hosts (respecting section_filter).
    section_keys: "dict" = {}
    for ip in ips:
        for sec, kvs in parsed[ip].items():
            if section_filter and sec != section_filter:
                continue
            section_keys.setdefault(sec, set()).update(kvs.keys())

    identical = 0
    differing: "list" = []

    for sec in sorted(section_keys):
        for key in sorted(section_keys[sec]):
            # Per-host raw value, or None if undefined.
            raws = [(ip, parsed[ip].get(sec, OrderedDict()).get(key))
                    for ip in ips]
            defined = [(ip, v) for ip, v in raws if v is not None]
            if not defined:
                continue
            norm_set = {normalize_value(key, v) for _, v in defined}
            if len(norm_set) == 1 and len(defined) == len(ips):
                identical += 1
                continue

            # Differing: group defined hosts by normalized value.
            groups_map: "dict" = {}  # normalized -> {value, hosts}
            missing_hosts: "list" = []
            for ip, raw in raws:
                if raw is None:
                    missing_hosts.append(ip)
                    continue
                nv = normalize_value(key, raw)
                bucket = groups_map.setdefault(
                    nv, {"value": raw, "hosts": []})
                bucket["hosts"].append(ip)

            groups = [
                {"value": g["value"], "normalized": nv,
                 "count": len(g["hosts"]), "hosts": g["hosts"]}
                for nv, g in groups_map.items()
            ]
            # Majority first (count desc), then by normalized value.
            groups.sort(key=lambda x: (-x["count"], x["normalized"]))
            differing.append({
                "section": sec,
                "key": key,
                "groups": groups,
                "missing_hosts": sorted(missing_hosts),
            })

    return {
        "identical_count": identical,
        "differing_count": len(differing),
        "differing": differing,
    }


def _disp_width(s: str) -> int:
    """Approximate display width: CJK/fullwidth chars count as 2 columns."""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
    return w


def _pad(s: str, width: int) -> str:
    """Left-justify s to `width` display columns (CJK-aware)."""
    return s + " " * max(0, width - _disp_width(s))


# ---------------------------------------------------------------------------
# Reporting: file vs file
# ---------------------------------------------------------------------------

def print_diff_report(
    files: typing.List[Path],
    section_filter: typing.Optional[str] = None,
    output_json: bool = False,
) -> dict:
    """Compare all config files at once (横向对比). Returns a structured result dict."""
    parsed: "dict" = {}
    all_duplicates: "dict" = {}
    ips: "list" = []
    for f in files:
        ip = extract_ip(f)
        ips.append(ip)
        content = f.read_text(encoding="utf-8", errors="replace")
        secs, dupes = parse_my_cnf(content, source_label=str(f), base_dir=f.parent)
        parsed[ip] = secs
        all_duplicates[ip] = dupes

    cross = build_cross_compare(parsed, ips, section_filter)

    result = {
        "mode": "file",
        "directory": str(files[0].parent.parent.parent) if files else "",
        "host_count": len(files),
        "hosts": {ip: sum(len(v) for v in parsed[ip].values()) for ip in ips},
        "duplicates": {ip: all_duplicates[ip] for ip in ips if all_duplicates[ip]},
        "cross_compare": cross,
    }

    if output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    # ---- Human-readable report ----
    print("=" * 72)
    print("  my.cnf 配置文件横向对比报告")
    print(f"  目录: {result['directory']}")
    print(f"  主机数: {len(files)}")
    if section_filter:
        print(f"  过滤 section: [{section_filter}]")
    print("=" * 72)
    print()

    print("--- 主机列表 ---")
    for ip in ips:
        print(f"  {ip}: {result['hosts'][ip]} 个参数")
    print()

    has_dupes = any(all_duplicates[ip] for ip in ips)
    if has_dupes:
        print("!!! 警告: 以下配置文件存在【重复 key】，MySQL 仅取最后一个值，")
        print("   前序值将被静默覆盖，请检查是否为笔误。")
        print("--- 重复 key 明细 ---")
        for ip in ips:
            if not all_duplicates[ip]:
                continue
            print(f"\n  [{ip}]")
            for w in all_duplicates[ip]:
                print(f"    ⚠  {w}")
        print()

    # ---- 横向差异（相同参数已忽略）----
    differing = cross["differing"]
    print("=" * 72)
    print("  横向差异（相同参数已忽略）")
    print(f"  相同参数 {cross['identical_count']} 个（已忽略），"
          f"差异/缺失参数 {cross['differing_count']} 个")
    print("=" * 72)

    if not differing:
        print("  所有参数在所有主机上完全一致。")
        print("\n--- 对比完成 ---")
        return result

    current_section = None
    for entry in differing:
        sec = entry["section"]
        if sec != current_section:
            current_section = sec
            print(f"\n  [{sec}]")
        print(f"    {entry['key']}")

        # Per-key column width so the ×N counts line up (CJK-aware).
        val_strs = [str(g["value"]) for g in entry["groups"]]
        if entry["missing_hosts"]:
            val_strs.append("(缺失)")
        width = max(_disp_width(s) for s in val_strs)

        for g in entry["groups"]:
            members = ", ".join(g["hosts"])
            print(f"        {_pad(str(g['value']), width)}  ×{g['count']}  {members}")
        if entry["missing_hosts"]:
            missing = ", ".join(entry["missing_hosts"])
            print(f"        {_pad('(缺失)', width)}  "
                  f"×{len(entry['missing_hosts'])}  {missing}")

    print()
    print("--- 对比完成 ---")
    return result


# ---------------------------------------------------------------------------
# Reporting: file vs running
# ---------------------------------------------------------------------------

def _connect_one(
    f: Path,
    mysql_port: int,
    mysql_user: typing.Optional[str],
    mysql_password: typing.Optional[str],
    socket_path: typing.Optional[str],
    ssl: typing.Optional[dict],
    connect_retries: int,
) -> typing.Tuple[str, "OrderedDict", "dict[str, str]", typing.Optional[str]]:
    """
    Worker for (optionally parallel) connection.

    Returns (ip, mysqld_section_params, running_vars, error_message).
    Returning running_vars alongside avoids a second connection in the
    comparison/best-practice phase.
    """
    ip = extract_ip(f)
    try:
        content = f.read_text(encoding="utf-8", errors="replace")
    except (OSError, IOError) as e:
        return ip, OrderedDict(), {}, f"无法读取文件 {f}: {e}"

    file_sections, _ = parse_my_cnf(content, base_dir=f.parent)
    mysqld_params = file_sections.get("mysqld", OrderedDict())

    running_vars, error = get_running_variables(
        host=ip, port=mysql_port, user=mysql_user,
        password=mysql_password, socket_path=socket_path, ssl=ssl,
        retries=connect_retries,
    )
    return ip, mysqld_params, running_vars, error


def _allow_diff_set(allow_diff_keys: typing.List[str]) -> typing.Set[str]:
    """Build the normalized set of keys exempt from discrepancy reporting.

    Keys are dash-normalized to underscore form to match the keys emitted by
    compare_file_vs_running (which works on the underscore-form cnf keys).
    """
    return {
        k.strip().replace("-", "_") for k in allow_diff_keys if k.strip()
    }


def _filter_allowed_diffs(
    discrepancies: typing.List[str],
    allow_set: typing.Set[str],
) -> typing.List[str]:
    """Drop discrepancies whose config key is in the --allow-diff set.

    Discrepancy strings emitted by compare_file_vs_running look like
    '文件配置 [key] = ...'; we match on the '文件配置 [key]' prefix.
    """
    return [
        d for d in discrepancies
        if not any(d.startswith(f"文件配置 [{k}]") for k in allow_set)
    ]


def run_running_vs_file_comparison(
    files: typing.List[Path],
    mysql_port: int,
    mysql_user: typing.Optional[str],
    mysql_password: typing.Optional[str],
    socket_path: typing.Optional[str],
    allow_diff_keys: typing.List[str],
    workers: int = 1,
    ssl: typing.Optional[dict] = None,
    connect_retries: int = 0,
    do_best_practice: bool = False,
    output_json: bool = False,
) -> dict:
    """Compare each host's file config with its running params."""
    allow_set = _allow_diff_set(allow_diff_keys)

    def do_one(f: Path):
        return _connect_one(
            f, mysql_port, mysql_user, mysql_password, socket_path,
            ssl, connect_retries,
        )

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            future_map = {ex.submit(do_one, f): f for f in files}
            done = {str(future_map[fut]): fut.result()
                    for fut in as_completed(future_map)}
        ordered = [done[str(f)] for f in files]
    else:
        ordered = [do_one(f) for f in files]

    all_discrepancies: dict[str, list[str]] = {}
    connection_errors: list[tuple[str, str]] = []
    best_practices: dict[str, list[tuple[str, str, str]]] = {}

    for (ip, mysqld_params, running_vars, error) in ordered:
        if error:
            connection_errors.append((ip, error))
            continue

        discrepancies = compare_file_vs_running(mysqld_params, running_vars)
        filtered = _filter_allowed_diffs(discrepancies, allow_set)
        if filtered:
            all_discrepancies[ip] = filtered

        if do_best_practice:
            bp = check_best_practices(running_vars)
            if bp:
                best_practices[ip] = bp

    total = sum(len(v) for v in all_discrepancies.values())

    result = {
        "mode": "file-vs-running",
        "port": mysql_port,
        "user": mysql_user or None,
        "connection_errors": [{"ip": ip, "error": e} for ip, e in connection_errors],
        "discrepancies": {ip: diffs for ip, diffs in all_discrepancies.items()},
        "best_practices": {
            ip: [{"severity": s, "variable": v, "message": m}
                 for s, v, m in findings]
            for ip, findings in best_practices.items()
        },
        "total_discrepancies": total,
        "hosts_compared": len(ordered) - len(connection_errors),
    }

    if output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    # ---- Human-readable ----
    print("=" * 72)
    print("  文件配置 vs 运行参数对比")
    print(f"  端口: {mysql_port}")
    print(f"  用户名: {mysql_user or '(未指定)'}")
    if workers > 1:
        print(f"  并发: {workers}")
    if ssl:
        print("  SSL: 启用")
    print("=" * 72)
    print()

    by_ip = {ip: (mysqld_params, running_vars, error)
             for (ip, mysqld_params, running_vars, error) in ordered}
    for f in files:
        ip = extract_ip(f)
        entry = by_ip.get(ip)
        if entry is None:
            continue
        _, _, error = entry
        print(f"--- 主机: {ip} ({f}) ---")
        if error:
            print(f"  ✗ 连接失败: {error}")
            continue
        diffs = all_discrepancies.get(ip, [])
        if diffs:
            for d in diffs:
                print(f"  ⚠  {d}")
        else:
            print("  ✓ 文件配置与运行参数一致")

    print()
    if connection_errors:
        print("--- 连接失败的主机 ---")
        for ip, error in connection_errors:
            print(f"  ✗ {ip}: {error}")
        print()

    if all_discrepancies:
        print("--- 参数差异汇总 ---")
        print(f"  共 {total} 处差异涉及 {len(all_discrepancies)} 个主机")
    elif not connection_errors:
        print("  ✓ 所有主机的文件配置与运行参数一致")
    print()

    if do_best_practice and best_practices:
        print("=" * 72)
        print("  最佳实践巡检")
        print("=" * 72)
        for ip, findings in best_practices.items():
            print(f"\n  [{ip}]")
            for severity, var, msg in findings:
                print(f"    {severity}: {var} — {msg}")
        print()

    return result


def run_single_host_comparison(
    config_file: Path,
    host: str,
    port: int,
    mysql_user: typing.Optional[str],
    mysql_password: typing.Optional[str],
    socket_path: typing.Optional[str],
    allow_diff_keys: typing.List[str],
    ssl: typing.Optional[dict] = None,
    connect_retries: int = 0,
    do_best_practice: bool = False,
    output_json: bool = False,
) -> dict:
    """
    Compare ONE config file against ONE explicitly-named instance.

    This is the file-vs-running-host mode: the target host/port come from the
    CLI (--mysql-host / --mysql-port), not from the file path. No directory
    scan, no multi-host file-vs-file report - strictly single-file vs
    single-instance.

    Returns a structured result dict whose shape mirrors
    run_running_vs_file_comparison so --json / --strict behave consistently.
    """
    allow_set = _allow_diff_set(allow_diff_keys)

    def _result(
        connection_errors: typing.List[dict],
        discrepancies: dict,
        best_practices: dict,
        total: int,
        hosts_compared: int,
    ) -> dict:
        return {
            "mode": "file-vs-running-host",
            "host": host,
            "port": port,
            "config_file": str(config_file),
            "connection_errors": connection_errors,
            "discrepancies": discrepancies,
            "best_practices": {
                h: [{"severity": s, "variable": v, "message": m}
                    for s, v, m in findings]
                for h, findings in best_practices.items()
            },
            "total_discrepancies": total,
            "hosts_compared": hosts_compared,
        }

    def _print_header() -> None:
        print("=" * 72)
        print("  文件配置 vs 运行参数对比（显式 host）")
        print(f"  目标: {host}:{port}")
        print(f"  配置文件: {config_file}")
        print(f"  用户名: {mysql_user or '(未指定)'}")
        if ssl:
            print("  SSL: 启用")
        print("=" * 72)
        print()

    # 1. Read & parse the single config file (includes resolve relative to it).
    try:
        content = config_file.read_text(encoding="utf-8", errors="replace")
    except (OSError, IOError) as e:
        err = f"无法读取文件 {config_file}: {e}"
        result = _result(
            connection_errors=[{"ip": host, "error": err}],
            discrepancies={}, best_practices={},
            total=0, hosts_compared=0,
        )
        if output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"✗ {err}")
        return result

    file_sections, _ = parse_my_cnf(content, base_dir=config_file.parent)
    mysqld_params = file_sections.get("mysqld", OrderedDict())

    # 2. Connect to the explicitly named instance.
    running_vars, error = get_running_variables(
        host=host, port=port, user=mysql_user,
        password=mysql_password, socket_path=socket_path, ssl=ssl,
        retries=connect_retries,
    )

    if error:
        result = _result(
            connection_errors=[{"ip": host, "error": error}],
            discrepancies={}, best_practices={},
            total=0, hosts_compared=0,
        )
        if output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_header()
            print(f"  ✗ 连接失败: {error}")
            print()
        return result

    # 3. Compare + optional best-practice audit.
    discrepancies = compare_file_vs_running(mysqld_params, running_vars)
    filtered = _filter_allowed_diffs(discrepancies, allow_set)

    best_practices: dict[str, list[tuple[str, str, str]]] = {}
    if do_best_practice:
        bp = check_best_practices(running_vars)
        if bp:
            best_practices[host] = bp

    result = _result(
        connection_errors=[],
        discrepancies={host: filtered} if filtered else {},
        best_practices=best_practices,
        total=len(filtered),
        hosts_compared=1,
    )

    if output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    # ---- Human-readable ----
    _print_header()
    if filtered:
        for d in filtered:
            print(f"  ⚠  {d}")
    else:
        print("  ✓ 文件配置与运行参数一致")
    print()

    if do_best_practice and best_practices:
        print("=" * 72)
        print("  最佳实践巡检")
        print("=" * 72)
        for h, findings in best_practices.items():
            print(f"\n  [{h}]")
            for severity, var, msg in findings:
                print(f"    {severity}: {var} - {msg}")
        print()

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_ssl_dict(args: argparse.Namespace) -> typing.Optional[dict]:
    """Build a PyMySQL ssl dict from CLI args, or None if SSL not requested."""
    if not args.ssl:
        return None
    ssl_cfg: dict = {}
    if args.ssl_ca:
        ssl_cfg["ca"] = args.ssl_ca
    if args.ssl_cert:
        ssl_cfg["cert"] = args.ssl_cert
    if args.ssl_key:
        ssl_cfg["key"] = args.ssl_key
    # An empty dict still tells PyMySQL to request SSL.
    return ssl_cfg or {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="对比多主机 my.cnf，可选对比文件配置与运行参数",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("base_directory", nargs="?",
                        help="配置文件基础目录（file / file-vs-running 模式必填；"
                             "file-vs-running-host 模式不使用）")
    parser.add_argument("--mode", choices=["file", "file-vs-running",
                                          "file-vs-running-host"],
                        default="file",
                        help="对比模式：file(仅文件两两对比) / "
                             "file-vs-running(目录扫描，从路径提取 IP 连各实例) / "
                             "file-vs-running-host(单个文件 vs 显式指定的单个实例)")
    parser.add_argument("--section", help="只对比指定 section（如 mysqld）")
    parser.add_argument("--config-path", help="配置文件路径模板（含 {ip} 占位符）")
    parser.add_argument("--config-name", action="append", default=None,
                        help="要匹配的配置文件名（可重复，默认 my.cnf/mysqld.cnf）")
    parser.add_argument("--mysql-port", type=int, default=3306,
                        help="MySQL 端口（默认 3306）")
    parser.add_argument("--mysql-config", help="MySQL 配置文件路径（含 [client] 节）")
    parser.add_argument("--password-conf", dest="password_conf",
                        help="凭证文件路径（[mysql] 节，password=/username=）"
                             "；优先级高于 --mysql-config")
    parser.add_argument("--mysql-host",
                        help="file-vs-running-host 模式必填：显式指定要连接的"
                             " MySQL 主机（IP/主机名）。其他模式从文件路径"
                             "提取 IP，此参数被忽略并警告")
    parser.add_argument("--config-file",
                        help="file-vs-running-host 模式必填：要对比的单个配置"
                             "文件路径（其他模式忽略）")
    parser.add_argument("--allow-diff", help="允许不同的参数列表（逗号分隔）")
    parser.add_argument("--workers", type=int, default=1,
                        help="并发连接数（默认 1）")
    parser.add_argument("--ssl", action="store_true", help="启用 SSL 连接")
    parser.add_argument("--ssl-ca", help="SSL CA 证书路径")
    parser.add_argument("--ssl-cert", help="SSL 客户端证书路径")
    parser.add_argument("--ssl-key", help="SSL 客户端私钥路径")
    parser.add_argument("--retries", type=int, default=0,
                        help="连接失败重试次数（默认 0）")
    parser.add_argument("--best-practice", action="store_true",
                        help="对运行参数做最佳实践/加固检查")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 输出结果（便于 CI 集成）")
    parser.add_argument("--strict", action="store_true",
                        help="发现差异或连接失败时以非零码退出")
    args = parser.parse_args()

    # Credentials / SSL are shared by both running modes; load once.
    mysql_user, mysql_password, resolved_port, socket_path = load_credentials(
        config_file=args.mysql_config,
        password_conf=args.password_conf,
    )
    allow_diff_keys = (
        [k.strip() for k in args.allow_diff.split(",") if k.strip()]
        if args.allow_diff else []
    )
    ssl = build_ssl_dict(args)
    exit_code = 0

    # ---- file-vs-running-host: explicit single host + single config file ----
    if args.mode == "file-vs-running-host":
        if not args.config_file:
            print("错误: 模式 'file-vs-running-host' 需要 --config-file 指定配置文件")
            return 1
        config_file = Path(args.config_file)
        if not config_file.is_file():
            print(f"错误: --config-file 不是有效文件: {config_file}")
            return 1
        if not args.mysql_host:
            print("错误: 模式 'file-vs-running-host' 需要 --mysql-host 指定目标实例")
            return 1
        if not (mysql_user or mysql_password):
            print("错误: 模式 'file-vs-running-host' 需要 MySQL 凭证"
                  "（--mysql-config / --password-conf / MYSQL_USER 环境变量）")
            return 1
        if args.base_directory:
            print(f"提示: 模式 'file-vs-running-host' 忽略 base_directory "
                  f"({args.base_directory})")
        if socket_path:
            print("提示: 模式 'file-vs-running-host' 按 host/port 连接，"
                  "忽略凭证文件里的 socket")
            socket_path = None
        if args.workers and args.workers > 1:
            print("提示: 模式 'file-vs-running-host' 为单实例，忽略 --workers")
        result = run_single_host_comparison(
            config_file,
            args.mysql_host,
            resolved_port or args.mysql_port,
            mysql_user, mysql_password, socket_path,
            allow_diff_keys,
            ssl=ssl,
            connect_retries=args.retries,
            do_best_practice=args.best_practice,
            output_json=args.json,
        )
        if args.strict and (result["total_discrepancies"] > 0
                            or result["connection_errors"]):
            exit_code = 2
        return exit_code

    # ---- file / file-vs-running: directory scan; base_directory required ----
    if not args.base_directory:
        print(f"错误: 模式 '{args.mode}' 需要 base_directory 参数")
        return 1
    if args.mysql_host:
        print("提示: --mysql-host 仅在 'file-vs-running-host' 模式生效，"
              "当前模式从文件路径提取 IP，已忽略")
    if args.config_file:
        print("提示: --config-file 仅在 'file-vs-running-host' 模式生效，已忽略")

    base_dir = os.path.abspath(args.base_directory)
    if not os.path.isdir(base_dir):
        print(f"错误: 目录不存在: {base_dir}")
        return 1

    config_names = (tuple(args.config_name) if args.config_name
                    else DEFAULT_CONFIG_NAMES)
    files = find_my_cnf_files(base_dir, args.config_path, config_names)
    if not files:
        print("未找到任何配置文件")
        return 1

    if args.mode == "file-vs-running":
        if not (mysql_user or mysql_password):
            print("错误: 模式 'file-vs-running' 需要 MySQL 凭证"
                  "（--mysql-config 或 MYSQL_USER/MYSQL_PASSWORD 环境变量）")
            return 1
        result = run_running_vs_file_comparison(
            files,
            resolved_port or args.mysql_port,
            mysql_user, mysql_password, socket_path,
            allow_diff_keys,
            workers=max(1, args.workers),
            ssl=ssl,
            connect_retries=args.retries,
            do_best_practice=args.best_practice,
            output_json=args.json,
        )
        if args.strict and (result["total_discrepancies"] > 0
                            or result["connection_errors"]):
            exit_code = 2

    # file-vs-file report (runs in both file / file-vs-running modes;
    # file-vs-running-host skips it - isolation, single file has no pairs)
    print_diff_report(
        files,
        section_filter=args.section,
        output_json=args.json,
    )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
