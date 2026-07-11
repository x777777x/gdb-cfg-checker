#!/usr/bin/env python3
"""
Test suite for the gdb_cfg_checker tool.

Covers:
- my.cnf parsing: sections, dash/underscore normalization, bare boolean
  options, inline comments, !include / !includedir (with cycle detection)
- value normalization: boolean / size / integer / path / string
- file-vs-file comparison with normalization (1G == 1073741824, etc.)
- file-vs-running comparison with correct type classification and
  read-only-variable exclusion
- best-practice checks
- IP extraction from paths

No live MySQL connection is required; the file-vs-running path is exercised
by supplying a fabricated running-vars dict directly.
"""

import os
import sys
import unittest
from collections import OrderedDict
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import compare_my_cnf as cmp  # noqa: E402
import mysql_checker as mc  # noqa: E402


class TestNormalization(unittest.TestCase):

    def test_boolean_normalization(self):
        for v in ("ON", "on", "1", "YES", "True", "yes"):
            self.assertEqual(mc.normalize_boolean(v), "ON", v)
        for v in ("OFF", "off", "0", "no", "FALSE", ""):
            self.assertEqual(mc.normalize_boolean(v), "OFF", v)

    def test_size_normalization(self):
        self.assertEqual(mc.normalize_numeric("1G"), str(1024 ** 3))
        self.assertEqual(mc.normalize_numeric("1024M"), str(1024 * 1024 ** 2))
        self.assertEqual(mc.normalize_numeric("1073741824"), "1073741824")
        self.assertEqual(mc.normalize_numeric("1g"), str(1024 ** 3))
        # non-numeric passes through
        self.assertEqual(mc.normalize_numeric("ROW"), "ROW")

    def test_integer_normalization(self):
        self.assertEqual(mc.normalize_integer("007"), "7")
        self.assertEqual(mc.normalize_integer("-1"), "-1")

    def test_path_normalization(self):
        self.assertEqual(mc.normalize_path("/data/db/"), "/data/db")
        self.assertEqual(mc.normalize_path('"/data/db"'), "/data/db")
        self.assertEqual(mc.normalize_path("/"), "/")  # root preserved

    def test_normalize_value_uses_spec_type(self):
        # binlog_format is STRING -> case-insensitive
        self.assertEqual(mc.normalize_value("binlog_format", "row"),
                         mc.normalize_value("binlog_format", "ROW"))
        # innodb_flush_log_at_trx_commit is INTEGER (not boolean)
        self.assertEqual(mc.normalize_value("innodb_flush_log_at_trx_commit", "1"),
                         mc.normalize_value("innodb_flush_log_at_trx_commit", "01"))
        # innodb_buffer_pool_size is SIZE
        self.assertEqual(mc.normalize_value("innodb_buffer_pool_size", "1G"),
                         mc.normalize_value("innodb_buffer_pool_size",
                                            "1073741824"))
        # lower_case_table_names is INTEGER, NOT boolean (so "2" stays "2")
        self.assertEqual(mc.normalize_value("lower_case_table_names", "2"), "2")


class TestMisclassificationFixed(unittest.TestCase):
    """Regression guard for the previously-misclassified parameters (#5)."""

    def test_binlog_format_is_string_not_boolean(self):
        # If it were boolean, 'ROW' would become 'ON'/'OFF' -> mismatch.
        self.assertEqual(mc.normalize_value("binlog_format", "ROW"), "row")

    def test_slave_parallel_type_is_string(self):
        self.assertEqual(mc.normalize_value("slave_parallel_type",
                                             "LOGICAL_SERVER"),
                         "logical_server")

    def test_innodb_flush_method_is_string(self):
        self.assertEqual(mc.normalize_value("innodb_flush_method", "O_DIRECT"),
                         "o_direct")

    def test_innodb_flush_log_at_trx_commit_is_integer(self):
        # value 2 must NOT collapse to boolean 'OFF'
        self.assertNotEqual(mc.normalize_value(
            "innodb_flush_log_at_trx_commit", "2"), "OFF")
        self.assertEqual(mc.normalize_value(
            "innodb_flush_log_at_trx_commit", "2"), "2")


class TestReadOnlyVarsExcluded(unittest.TestCase):
    """version / innodb_version / hostname must not be compared (#6)."""

    def test_compare_skips_readonly(self):
        file_cfg = {"version": "8.0.32"}  # nonsensical in a file, but tests guard
        running = {"version": "8.0.31"}
        self.assertEqual(mc.compare_file_vs_running(file_cfg, running), [])


class TestParseMyCnf(unittest.TestCase):

    def test_dash_to_underscore(self):
        content = "[mysqld]\nlog-bin = mysql-bin\nskip-name-resolve\n"
        secs, _ = cmp.parse_my_cnf(content)
        self.assertIn("log_bin", secs["mysqld"])
        self.assertIn("skip_name_resolve", secs["mysqld"])

    def test_bare_boolean_option(self):
        content = "[mysqld]\nlog-bin\nskip-name-resolve\nsymbolic-links\n"
        secs, _ = cmp.parse_my_cnf(content)
        self.assertEqual(secs["mysqld"]["log_bin"], "ON")
        self.assertEqual(secs["mysqld"]["skip_name_resolve"], "ON")
        self.assertEqual(secs["mysqld"]["symbolic_links"], "ON")

    def test_inline_comment_stripped(self):
        content = "[mysqld]\nmax_connections = 200   # limit\nport=3306 ; note\n"
        secs, _ = cmp.parse_my_cnf(content)
        self.assertEqual(secs["mysqld"]["max_connections"], "200")
        self.assertEqual(secs["mysqld"]["port"], "3306")

    def test_value_with_hash_not_stripped(self):
        # '#' with no preceding space is part of the value (rare but real).
        content = "[mysqld]\nsome_pass = ab#cd\n"
        secs, _ = cmp.parse_my_cnf(content)
        self.assertEqual(secs["mysqld"]["some_pass"], "ab#cd")

    def test_duplicate_key_warned_last_wins(self):
        content = "[mysqld]\nmax_connections = 100\nmax_connections = 200\n"
        secs, dups = cmp.parse_my_cnf(content)
        self.assertEqual(secs["mysqld"]["max_connections"], "200")
        self.assertEqual(len(dups), 1)

    def test_section_isolation(self):
        content = ("[client]\nport = 3307\n[mysqld]\nport = 3306\n")
        secs, _ = cmp.parse_my_cnf(content)
        self.assertEqual(secs["client"]["port"], "3307")
        self.assertEqual(secs["mysqld"]["port"], "3306")

    def test_pre_section_directive_ignored(self):
        content = "loose_magic = 1\n[mysqld]\nport = 3306\n"
        secs, _ = cmp.parse_my_cnf(content)
        self.assertEqual(secs["mysqld"]["port"], "3306")


class TestInclude(unittest.TestCase):

    def _write(self, path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_include_merge(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            extra = td / "extra.cnf"
            self._write(extra, "[mysqld]\nbinlog_format = ROW\n")
            main = td / "my.cnf"
            self._write(main,
                        f"[mysqld]\n!include {extra}\nmax_connections = 200\n")
            secs, _ = cmp.parse_my_cnf(
                main.read_text(), base_dir=main.parent)
            self.assertEqual(secs["mysqld"]["binlog_format"], "ROW")
            self.assertEqual(secs["mysqld"]["max_connections"], "200")

    def test_includedir_relative(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            incdir = td / "conf.d"
            self._write(incdir / "a.cnf", "[mysqld]\nport = 3307\n")
            self._write(incdir / "b.cnf", "[mysqld]\nbinlog_format = ROW\n")
            main = td / "my.cnf"
            self._write(main,
                        "[mysqld]\n!includedir conf.d\nmax_connections = 200\n")
            secs, _ = cmp.parse_my_cnf(
                main.read_text(), base_dir=main.parent)
            self.assertEqual(secs["mysqld"]["port"], "3307")
            self.assertEqual(secs["mysqld"]["binlog_format"], "ROW")

    def test_include_cycle_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            a = td / "a.cnf"
            b = td / "b.cnf"
            self._write(a, f"[mysqld]\n!include {b}\nport=3306\n")
            self._write(b, f"[mysqld]\n!include {a}\nmax_connections=200\n")
            secs, dups = cmp.parse_my_cnf(a.read_text(), base_dir=a.parent)
            # Both keys still resolve (last-wins across the merge).
            self.assertIn("port", secs["mysqld"])
            self.assertTrue(any("环路" in d for d in dups))


class TestCrossCompare(unittest.TestCase):
    """build_cross_compare: all hosts at once, identical ignored, differing grouped."""

    def _parsed(self, per_ip):
        # per_ip: {ip: {section: {key: value}}}
        d = {}
        for ip, secs in per_ip.items():
            od = OrderedDict()
            for sec, kvs in secs.items():
                od[sec] = OrderedDict(kvs)
            d[ip] = od
        return d

    def test_all_identical_ignored(self):
        parsed = self._parsed({
            "10.0.0.1": {"mysqld": {"max_connections": "200", "log_bin": "ON"}},
            "10.0.0.2": {"mysqld": {"max_connections": "200", "log_bin": "ON"}},
        })
        r = cmp.build_cross_compare(parsed, ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(r["differing"], [])
        self.assertEqual(r["differing_count"], 0)
        self.assertEqual(r["identical_count"], 2)

    def test_diff_grouped_by_value_majority_first(self):
        parsed = self._parsed({
            "10.0.0.1": {"mysqld": {"max_connections": "200"}},
            "10.0.0.2": {"mysqld": {"max_connections": "200"}},
            "10.0.0.3": {"mysqld": {"max_connections": "300"}},
        })
        r = cmp.build_cross_compare(parsed, ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertEqual(r["differing_count"], 1)
        entry = r["differing"][0]
        self.assertEqual(entry["key"], "max_connections")
        # majority (count 2) first, then the outlier
        self.assertEqual(entry["groups"][0]["count"], 2)
        self.assertEqual(entry["groups"][0]["value"], "200")
        self.assertEqual(entry["groups"][1]["count"], 1)
        self.assertEqual(entry["groups"][1]["value"], "300")
        self.assertEqual(entry["missing_hosts"], [])

    def test_normalization_merges_equivalent(self):
        parsed = self._parsed({
            "10.0.0.1": {"mysqld": {"innodb_buffer_pool_size": "1G",
                                    "log_bin": "ON"}},
            "10.0.0.2": {"mysqld": {"innodb_buffer_pool_size": "1073741824",
                                    "log_bin": "on"}},
        })
        r = cmp.build_cross_compare(parsed, ["10.0.0.1", "10.0.0.2"])
        # 1G==1073741824, ON==on -> identical, not differing
        self.assertEqual(r["differing"], [])
        self.assertEqual(r["identical_count"], 2)

    def test_missing_hosts_reported(self):
        parsed = self._parsed({
            "10.0.0.1": {"mysqld": {"server_id": "1", "relay_log": "on"}},
            "10.0.0.2": {"mysqld": {"server_id": "2"}},  # relay_log missing
        })
        r = cmp.build_cross_compare(parsed, ["10.0.0.1", "10.0.0.2"])
        keys = {e["key"]: e for e in r["differing"]}
        self.assertIn("server_id", keys)
        self.assertIn("relay_log", keys)
        self.assertEqual(keys["relay_log"]["missing_hosts"], ["10.0.0.2"])
        self.assertEqual(keys["relay_log"]["groups"][0]["count"], 1)

    def test_section_filter(self):
        parsed = self._parsed({
            "10.0.0.1": {"client": {"port": "3307"}, "mysqld": {"port": "3306"}},
            "10.0.0.2": {"client": {"port": "3306"}, "mysqld": {"port": "3306"}},
        })
        # filter to mysqld -> client's differing port is ignored
        r = cmp.build_cross_compare(parsed, ["10.0.0.1", "10.0.0.2"],
                                    section_filter="mysqld")
        self.assertEqual(r["differing"], [])
        # without filter, client port differs
        r_all = cmp.build_cross_compare(parsed, ["10.0.0.1", "10.0.0.2"])
        self.assertGreaterEqual(r_all["differing_count"], 1)


class TestCompareFileVsRunning(unittest.TestCase):

    def test_no_false_positives(self):
        file_cfg = {
            "max_connections": "200",
            "innodb_buffer_pool_size": "1G",
            "log_bin": "ON",
            "binlog_format": "row",
            "innodb_flush_log_at_trx_commit": "1",
            "lower_case_table_names": "1",
            "datadir": "/data/db/",
        }
        running = {
            "max_connections": "200",
            "innodb_buffer_pool_size": "1073741824",
            "log_bin": "ON",
            "binlog_format": "ROW",
            "innodb_flush_log_at_trx_commit": "1",
            "lower_case_table_names": "1",
            "datadir": "/data/db",
        }
        self.assertEqual(mc.compare_file_vs_running(file_cfg, running), [])

    def test_missing_in_running_reported(self):
        file_cfg = {"max_connections": "200"}
        self.assertEqual(len(mc.compare_file_vs_running(file_cfg, {})), 1)

    def test_mismatch_reported(self):
        file_cfg = {"max_connections": "200"}
        running = {"max_connections": "300"}
        diffs = mc.compare_file_vs_running(file_cfg, running)
        self.assertEqual(len(diffs), 1)


class TestBestPractices(unittest.TestCase):

    def test_weak_config_flagged(self):
        weak = {
            "innodb_flush_log_at_trx_commit": "2",
            "sync_binlog": "0",
            "binlog_format": "STATEMENT",
            "log_bin": "OFF",
            "skip_name_resolve": "OFF",
            "local_infile": "ON",
            "max_connections": "5",
            "max_connect_errors": "10",
        }
        findings = mc.check_best_practices(weak)
        flagged = {v for _, v, _ in findings}
        self.assertIn("innodb_flush_log_at_trx_commit", flagged)
        self.assertIn("sync_binlog", flagged)
        self.assertIn("binlog_format", flagged)
        self.assertIn("skip_name_resolve", flagged)
        self.assertIn("local_infile", flagged)
        self.assertIn("max_connect_errors", flagged)
        self.assertIn("max_connections", flagged)

    def test_strong_config_clean(self):
        strong = {
            "innodb_flush_log_at_trx_commit": "1",
            "sync_binlog": "1",
            "binlog_format": "ROW",
            "log_bin": "ON",
            "skip_name_resolve": "ON",
            "local_infile": "OFF",
            "max_connections": "500",
            "max_connect_errors": "1000",
            "innodb_buffer_pool_size": "4G",
        }
        # Only INFO-level informational findings allowed (e.g. buffer pool
        # size note); no WARN.
        warns = [f for f in mc.check_best_practices(strong) if f[0] == "WARN"]
        self.assertEqual(warns, [])


class TestIpExtraction(unittest.TestCase):

    def test_extract_before_data(self):
        p = Path("/data/goldendb/10.0.1.1/data/goldendb/db1/etc/my.cnf")
        self.assertEqual(cmp.extract_ip(p), "10.0.1.1")

    def test_extract_when_base_path_contains_data(self):
        # Regression: when the base directory itself contains a 'data'
        # segment (real GoldenDB: /data/goldendb/<ip>/data/...), the first
        # 'data' must not be mistaken for the per-host marker.
        p = Path("/data/goldendb/192.168.1.5/data/goldendb/db1/etc/my.cnf")
        self.assertEqual(cmp.extract_ip(p), "192.168.1.5")

    def test_extract_ipv4_segment(self):
        p = Path("/opt/192.168.0.5/conf/my.cnf")
        self.assertEqual(cmp.extract_ip(p), "192.168.0.5")

    def test_extract_fallback_parent(self):
        p = Path("/opt/hostA/my.cnf")
        self.assertEqual(cmp.extract_ip(p), "hostA")


class TestConfigNameFilter(unittest.TestCase):
    """The goldendb hardcoding (#8/四-2) is removed; matching is by name."""

    def test_finds_non_goldendb_paths(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "10.0.0.1").mkdir(parents=True)
            (td / "10.0.0.1" / "etc").mkdir()
            (td / "10.0.0.1" / "etc" / "mysqld.cnf").write_text(
                "[mysqld]\nport=3306\n")
            files = cmp.find_my_cnf_files(str(td), config_names=("mysqld.cnf",))
            self.assertEqual(len(files), 1)


class TestPasswordConf(unittest.TestCase):
    """--password-conf reads [mysql] section with username=/password= keys."""

    def _write(self, path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_mysql_section_username_key(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = self._write(Path(td) / "creds.cnf",
                            "[mysql]\nusername=root\npassword=s3cret\n")
            u, p, port, sock = mc._parse_mysql_config_file(
                str(f), sections=("client", "mysql"),
                user_keys=("user", "username"))
            self.assertEqual(u, "root")
            self.assertEqual(p, "s3cret")

    def test_mysql_overrides_client_in_same_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = self._write(Path(td) / "creds.cnf",
                            "[client]\nuser=cli_user\npassword=cli_pw\n"
                            "[mysql]\nusername=db_user\npassword=db_pw\n")
            u, p, _, _ = mc._parse_mysql_config_file(
                str(f), sections=("client", "mysql"),
                user_keys=("user", "username"))
            # [mysql] has higher priority -> wins
            self.assertEqual(u, "db_user")
            self.assertEqual(p, "db_pw")

    def test_load_credentials_password_conf_overrides_mysql_config(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pw = self._write(Path(td) / "pw.cnf",
                             "[mysql]\nusername=admin\npassword=adm_pw\n")
            mc_cfg = self._write(Path(td) / "my.cnf",
                                 "[client]\nuser=cli_user\npassword=cli_pw\n")
            u, p, _, _ = mc.load_credentials(
                config_file=str(mc_cfg), password_conf=str(pw))
            self.assertEqual(u, "admin")
            self.assertEqual(p, "adm_pw")

    def test_load_credentials_env_fallback(self):
        os.environ["MYSQL_USER"] = "env_user"
        os.environ["MYSQL_PASSWORD"] = "env_pw"
        try:
            u, p, _, _ = mc.load_credentials()
            self.assertEqual(u, "env_user")
            self.assertEqual(p, "env_pw")
        finally:
            del os.environ["MYSQL_USER"]
            del os.environ["MYSQL_PASSWORD"]


class TestSingleHostMode(unittest.TestCase):
    """file-vs-running-host: one config file vs one explicitly named instance.

    get_running_variables is monkeypatched so no live MySQL is needed.
    """

    def _write(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    @contextmanager
    def _quiet(self):
        """Suppress stdout so test runs don't drown in report output."""
        buf = StringIO()
        with patch("sys.stdout", new=buf):
            yield buf

    # ---- function-level: run_single_host_comparison ----

    def _run(self, f: Path, running: dict, error=None, **kw):
        with patch("compare_my_cnf.get_running_variables",
                   return_value=(running, error)):
            return cmp.run_single_host_comparison(
                f, "127.0.0.1", 3306, "root", "pw", None, [], **kw)

    def test_consistent_no_diff(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, self._quiet():
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\nmax_connections=200\nlog_bin=ON\n")
            r = self._run(f, {"max_connections": "200", "log_bin": "ON"})
            self.assertEqual(r["total_discrepancies"], 0)
            self.assertEqual(r["hosts_compared"], 1)
            self.assertEqual(r["connection_errors"], [])
            self.assertEqual(r["mode"], "file-vs-running-host")
            self.assertNotIn("127.0.0.1", r["discrepancies"])

    def test_discrepancy_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, self._quiet():
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\nmax_connections=200\n")
            r = self._run(f, {"max_connections": "300"})
            self.assertEqual(r["total_discrepancies"], 1)
            self.assertIn("127.0.0.1", r["discrepancies"])

    def test_size_normalization_no_false_positive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, self._quiet():
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\ninnodb_buffer_pool_size=1G\n")
            r = self._run(f, {"innodb_buffer_pool_size": "1073741824"})
            self.assertEqual(r["total_discrepancies"], 0)

    def test_connection_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, self._quiet():
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\nmax_connections=200\n")
            r = self._run(f, {}, error="连接 127.0.0.1:3306 失败")
            self.assertEqual(r["hosts_compared"], 0)
            self.assertEqual(len(r["connection_errors"]), 1)
            self.assertEqual(r["total_discrepancies"], 0)

    def test_read_error_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, self._quiet():
            # File does not exist -> read_text raises (defensive: the CLI
            # normally guards with is_file(), but the function must cope).
            f = Path(td) / "missing.cnf"
            r = self._run(f, {})
            self.assertEqual(r["hosts_compared"], 0)
            self.assertEqual(len(r["connection_errors"]), 1)

    def test_allow_diff_filters(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, self._quiet():
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\nmax_connections=200\nport=3306\n")
            running = {"max_connections": "300", "port": "3307"}
            with patch("compare_my_cnf.get_running_variables",
                       return_value=(running, None)):
                r = cmp.run_single_host_comparison(
                    f, "127.0.0.1", 3306, "root", "pw", None, ["port"])
            # port is allow-listed -> only max_connections remains
            self.assertEqual(r["total_discrepancies"], 1)
            self.assertTrue(any("max_connections" in d
                                for d in r["discrepancies"]["127.0.0.1"]))

    def test_best_practice(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, self._quiet():
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\nmax_connections=200\n")
            running = {"max_connections": "200", "sync_binlog": "0",
                       "innodb_buffer_pool_size": "134217728"}
            r = self._run(f, running, do_best_practice=True)
            self.assertIn("127.0.0.1", r["best_practices"])
            flagged = {b["variable"]
                       for b in r["best_practices"]["127.0.0.1"]}
            self.assertIn("sync_binlog", flagged)

    # ---- CLI validation via main() ----

    def _cli(self, argv, creds=("root", "pw"),
             running=None, capture_hosts=False):
        """Run main() with patched argv/creds/connection. Return (rc, out[, hosts])."""
        running = {} if running is None else running
        seen_hosts = []
        def fake_get(**kw):
            seen_hosts.append(kw.get("host"))
            return (running, None)
        buf = StringIO()
        patches = [
            patch.object(sys, "argv", argv),
            patch("sys.stdout", new=buf),
            patch("compare_my_cnf.load_credentials",
                  return_value=(creds[0], creds[1], None, None)),
            patch("compare_my_cnf.get_running_variables", side_effect=fake_get),
        ]
        for p in patches:
            p.start()
        try:
            rc = cmp.main()
        finally:
            for p in patches:
                p.stop()
        out = buf.getvalue()
        if capture_hosts:
            return rc, out, seen_hosts
        return rc, out

    def test_cli_missing_config_file(self):
        rc, out = self._cli(["prog", "--mode", "file-vs-running-host",
                             "--mysql-host", "127.0.0.1"])
        self.assertEqual(rc, 1)
        self.assertIn("--config-file", out)

    def test_cli_config_file_is_directory(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            rc, out = self._cli(["prog", "--mode", "file-vs-running-host",
                                 "--config-file", td,
                                 "--mysql-host", "127.0.0.1"])
            self.assertEqual(rc, 1)
            self.assertIn("不是有效文件", out)

    def test_cli_missing_mysql_host(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\nmax_connections=200\n")
            rc, out = self._cli(["prog", "--mode", "file-vs-running-host",
                                 "--config-file", str(f)])
            self.assertEqual(rc, 1)
            self.assertIn("--mysql-host", out)

    def test_cli_missing_credentials(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\nmax_connections=200\n")
            rc, out = self._cli(["prog", "--mode", "file-vs-running-host",
                                 "--config-file", str(f),
                                 "--mysql-host", "127.0.0.1"],
                                creds=(None, None))
            self.assertEqual(rc, 1)
            self.assertIn("凭证", out)

    def test_cli_success_exit_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\nmax_connections=200\n")
            rc, out = self._cli(["prog", "--mode", "file-vs-running-host",
                                 "--config-file", str(f),
                                 "--mysql-host", "127.0.0.1"],
                                running={"max_connections": "200"})
            self.assertEqual(rc, 0)
            self.assertIn("一致", out)

    def test_cli_strict_exit_two_on_diff(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = self._write(Path(td) / "my.cnf",
                            "[mysqld]\nmax_connections=200\n")
            rc, out = self._cli(["prog", "--mode", "file-vs-running-host",
                                 "--config-file", str(f),
                                 "--mysql-host", "127.0.0.1",
                                 "--strict", "--json"],
                                running={"max_connections": "300"})
            self.assertEqual(rc, 2)

    def test_cli_file_vs_running_ignores_mysql_host(self):
        """In file-vs-running mode, --mysql-host must NOT override path IP."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "127.0.0.1").mkdir()
            (Path(td) / "127.0.0.1" / "my.cnf").write_text(
                "[mysqld]\nmax_connections=200\n")
            rc, out, hosts = self._cli(
                ["prog", td, "--mode", "file-vs-running",
                 "--mysql-host", "9.9.9.9"],
                running={"max_connections": "200"}, capture_hosts=True)
            self.assertEqual(rc, 0)
            self.assertIn("已忽略", out)
            self.assertIn("127.0.0.1", hosts)
            self.assertNotIn("9.9.9.9", hosts)


if __name__ == "__main__":
    unittest.main()
