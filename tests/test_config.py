import importlib
import os
import unittest
from unittest.mock import patch


class ConfigDatabaseUriTests(unittest.TestCase):
    def test_relative_sqlite_url_is_resolved_against_project_root(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///database/attendance.db"}, clear=False):
            import backend.config as config_module

            importlib.reload(config_module)

            self.assertTrue(config_module.Config.SQLALCHEMY_DATABASE_URI.startswith("sqlite:///"))
            self.assertIn(str(config_module.BASE_DIR), config_module.Config.SQLALCHEMY_DATABASE_URI)
            self.assertNotEqual(config_module.Config.SQLALCHEMY_DATABASE_URI, "sqlite:///database/attendance.db")


if __name__ == "__main__":
    unittest.main()
