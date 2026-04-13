import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name, path, stubs=None):
    """Import a module from disk while injecting lightweight dependency stubs."""
    saved = {}
    try:
        for stub_name, stub_module in (stubs or {}).items():
            saved[stub_name] = sys.modules.get(stub_name)
            sys.modules[stub_name] = stub_module

        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for stub_name, previous in saved.items():
            if previous is None:
                sys.modules.pop(stub_name, None)
            else:
                sys.modules[stub_name] = previous


def build_etoken_stubs():
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda dotenv_path=None: None

    playwright = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")

    async def async_playwright():
        raise AssertionError("async_playwright should not be used in these unit tests")

    async_api.async_playwright = async_playwright
    playwright.async_api = async_api
    return {
        "dotenv": dotenv,
        "playwright": playwright,
        "playwright.async_api": async_api,
    }


def build_webapp_stubs():
    flask = types.ModuleType("flask")

    class DummyFlask:
        def __init__(self, *args, **kwargs):
            pass

        def route(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

    flask.Flask = DummyFlask
    flask.render_template = lambda *args, **kwargs: None
    flask.request = types.SimpleNamespace(form={})
    flask.jsonify = lambda payload: payload

    etoken_monitor = types.ModuleType("etoken_monitor")
    etoken_monitor.run_monitor = object()

    return {
        "flask": flask,
        "etoken_monitor": etoken_monitor,
    }


class MonitorStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.monitor = load_module(
            "etoken_monitor_test",
            REPO_ROOT / "etoken_monitor.py",
            build_etoken_stubs(),
        )
        cls.webapp = load_module(
            "webapp_test",
            REPO_ROOT / "webapp.py",
            build_webapp_stubs(),
        )

    def test_partial_result_is_classified_as_pending_confirmation(self):
        result = {
            "Source Site Entry Record:": "ENTRY-123",
            "E-Token Generated @": "2026-04-13 09:00:00",
        }
        self.assertEqual(
            self.monitor.classify_generation_result(result),
            self.monitor.STATUS_PENDING_CONFIRMATION,
        )

    def test_unrelated_table_data_does_not_count_as_processing(self):
        result = {"Some Other Label": "unexpected"}
        self.assertEqual(
            self.monitor.classify_generation_result(result),
            self.monitor.STATUS_FAILED,
        )

    def test_save_token_updates_existing_processing_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tokens_path = Path(tmpdir) / "tokens.json"
            tokens_path.write_text(
                json.dumps(
                    [
                        {
                            "timestamp": "2026-04-13T09:00:00",
                            "truck_no": "XF68P",
                            "material": "GOODEARTH",
                            "token": "",
                            "site": "CR202",
                            "generated_at": "2026-04-13 09:00:00",
                            "entry_record": "ENTRY-123",
                            "status": self.monitor.STATUS_PROCESSING,
                            "message": "Submission appears accepted",
                        }
                    ]
                )
            )

            self.monitor.TOKENS_FILE = tokens_path
            self.monitor._tokens_lock = None

            token_data = self.monitor.build_token_record(
                "XF68P",
                "GOODEARTH",
                {
                    self.monitor.RESULT_TOKEN_LABEL: "TK-999",
                    "Source Site Entry Record:": "ENTRY-123",
                    "E-Token Generated @": "2026-04-13 09:00:05",
                },
                status=self.monitor.STATUS_SUCCESS,
                timestamp="2026-04-13T09:00:05",
            )

            asyncio.run(self.monitor.save_token(token_data))

            saved_rows = json.loads(tokens_path.read_text())
            self.assertEqual(len(saved_rows), 1)
            self.assertEqual(saved_rows[0]["status"], self.monitor.STATUS_SUCCESS)
            self.assertEqual(saved_rows[0]["token"], "TK-999")
            self.assertEqual(saved_rows[0]["entry_record"], "ENTRY-123")

    def test_save_token_keeps_existing_metadata_when_update_is_sparse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tokens_path = Path(tmpdir) / "tokens.json"
            tokens_path.write_text(
                json.dumps(
                    [
                        {
                            "timestamp": "2026-04-13T09:00:00",
                            "truck_no": "XF68P",
                            "material": "GOODEARTH",
                            "token": "",
                            "site": "CR202",
                            "generated_at": "2026-04-13 09:00:00",
                            "entry_record": "ENTRY-123",
                            "status": self.monitor.STATUS_PROCESSING,
                            "message": "Initial pending state",
                        }
                    ]
                )
            )

            self.monitor.TOKENS_FILE = tokens_path
            self.monitor._tokens_lock = None

            sparse_update = self.monitor.build_token_record(
                "XF68P",
                "GOODEARTH",
                {},
                status=self.monitor.STATUS_PROCESSING,
                message="Still processing on server",
                timestamp="2026-04-13T09:00:10",
            )

            asyncio.run(self.monitor.save_token(sparse_update))

            saved_rows = json.loads(tokens_path.read_text())
            self.assertEqual(saved_rows[0]["entry_record"], "ENTRY-123")
            self.assertEqual(saved_rows[0]["generated_at"], "2026-04-13 09:00:00")
            self.assertEqual(saved_rows[0]["message"], "Still processing on server")

    def test_dashboard_includes_processing_rows(self):
        self.assertTrue(
            self.webapp.should_include_token_record(
                {"token": "", "status": "processing"}
            )
        )
        self.assertFalse(
            self.webapp.should_include_token_record({"token": "", "status": "failed"})
        )

    def test_persisted_config_round_trips_through_env_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("# keep me\nEXTRA_KEY=untouched\n")
            self.webapp.ENV_FILE = env_path

            config = {
                "ETOKEN_USERNAME": "demo-user",
                "ETOKEN_PASSWORD": 'p@ss "word"',
                "TRUCK_NO": "XF68P,XF99G",
                "MATERIAL": "GOODEARTH",
                "CYCLE_INTERVAL": "5",
                "START_TIME": "08:00",
                "END_TIME": "18:00",
            }

            self.webapp.save_persisted_config(config)
            loaded = self.webapp.load_persisted_config()

            self.assertEqual(loaded, config)
            env_text = env_path.read_text()
            self.assertIn("EXTRA_KEY=untouched", env_text)
            self.assertIn('ETOKEN_USERNAME="demo-user"', env_text)


if __name__ == "__main__":
    unittest.main()
