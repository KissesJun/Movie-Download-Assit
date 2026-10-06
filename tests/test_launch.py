import contextlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from launch import bind_listener, browser_url, check_database, install_config, load_settings, main


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {
            "server": {"host": "127.0.0.1", "port": 5000},
            "qbittorrent": {"host": "localhost", "port": 8080, "username": "user", "password": "private-test-password"},
            "download": {"complete_root": "/downloads", "temp_root": "D:/temp"},
            "display": {"timezone": "UTC"},
        }
        self.save()

    def save(self):
        (self.root / "config.json").write_text(json.dumps(self.settings), encoding="utf-8")

    def test_setup_preserves_existing_config(self):
        original = (self.root / "config.json").read_bytes()
        self.assertFalse(install_config(self.root))
        self.assertEqual(original, (self.root / "config.json").read_bytes())
        with tempfile.TemporaryDirectory() as new:
            root = Path(new)
            (root / "config.example.json").write_bytes(original)
            self.assertTrue(install_config(root))
            self.assertEqual(original, (root / "config.json").read_bytes())

    def test_invalid_config_has_actionable_message_without_secrets(self):
        self.settings["qbittorrent"]["password"] = "CHANGE_ME"
        self.save()
        with self.assertRaisesRegex(ValueError, "WebUI"):
            load_settings(self.root)
        (self.root / "config.json").write_text('{"password":"private-test-password",}', encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            load_settings(self.root)
        self.assertIn("第 1 行", str(caught.exception))
        self.assertNotIn("private-test-password", str(caught.exception))
        self.settings["server"]["port"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "server.port"):
            load_settings(self.root)

    def test_port_in_use_fails_before_opening_database(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            self.settings["server"]["port"] = occupied.getsockname()[1]
            self.save()
            with patch("launch.check_dependencies"), patch("launch.check_database") as database:
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(main(["--check"], self.root), 1)
                self.assertIn("不要重复启动", output.getvalue())
                database.assert_not_called()

    def test_backup_preserves_wal_data_and_never_upgrades_source(self):
        folder = self.root / "data"
        folder.mkdir()
        path = folder / "app.db"
        with contextlib.closing(sqlite3.connect(path)) as source:
            source.execute("PRAGMA journal_mode=WAL")
            source.execute("PRAGMA user_version=2")
            source.execute("CREATE TABLE history (value TEXT)")
            source.execute("INSERT INTO history VALUES ('old-record')")
            source.commit()
            self.assertIsNone(check_database(self.root, backup=False))
            self.assertFalse((folder / "backups").exists())
            backup = check_database(self.root, backup=True)
            with contextlib.closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(saved.execute("SELECT value FROM history").fetchone()[0], "old-record")
                self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(source.execute("PRAGMA user_version").fetchone()[0], 2)
            source.execute("PRAGMA user_version=3")
            self.assertIsNone(check_database(self.root, backup=True))

    def test_newer_database_rejected_and_wildcard_browser_addresses(self):
        (self.root / "data").mkdir()
        with contextlib.closing(sqlite3.connect(self.root / "data" / "app.db")) as db:
            db.execute("PRAGMA user_version=4")
        with self.assertRaisesRegex(ValueError, "数据库版本"):
            check_database(self.root)
        self.assertEqual(browser_url({"host": "0.0.0.0", "port": 5001}), "http://127.0.0.1:5001")
        self.assertEqual(browser_url({"host": "::", "port": 5001}), "http://[::1]:5001")
