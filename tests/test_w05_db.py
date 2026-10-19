"""W5 離線測試：用假的 psycopg2 檢查參數化 SQL、主鍵判斷重複、錯誤不外洩；不連真的資料庫。"""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("w05_service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

PASSWORD = "pw-should-never-leak-0123456789"


class FakeError(Exception):
    pass


class FakeDb:
    def __init__(self):
        self.rows, self.log, self.down = {}, [], False


class FakeCursor:
    def __init__(self, db):
        self.db, self.description, self.result = db, None, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.db.log.append((sql, params))
        words = " ".join(sql.split())
        self.description, self.result = None, []
        if words.startswith("INSERT"):
            self.description = True
            if params[0] not in self.db.rows:  # ON CONFLICT DO NOTHING：衝突時不回傳列
                row = (*params, datetime.now(timezone.utc))
                self.db.rows[params[0]] = row
                self.result = [row]
        elif words.startswith("SELECT") and "WHERE event_id = %s" in words:
            self.description = True
            self.result = [self.db.rows[params[0]]] if params[0] in self.db.rows else []
        elif words.startswith("SELECT"):
            self.description = True
            self.result = sorted(self.db.rows.values(), key=lambda r: r[5], reverse=True)[:params[-1]]

    def fetchall(self):
        return self.result


class FakeConn:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.db)

    def close(self):
        pass


def fake_module(db):
    def connect(**params):
        assert params["sslmode"] == "verify-full" and params["sslrootcert"].endswith("rds-ca.pem")
        if db.down:
            raise FakeError(f"could not connect password={params['password']}")
        return FakeConn(db)
    return types.SimpleNamespace(connect=connect, Error=FakeError)


class PgStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        self._saved = sys.modules.get("psycopg2")
        sys.modules["psycopg2"] = fake_module(self.db)
        self.store = service.PgStore({"DB_HOST": "db.example", "DB_NAME": "inspection", "DB_USER": "app",
                                      "DB_PASSWORD": PASSWORD})
        self.event = service.validate({"event_id": "g06-m01-0201", "device_id": "g06-m01-d01",
                                       "observed_at": "2026-10-19T22:00:00+08:00", "type": "status",
                                       "note": "it's ok'); DROP TABLE events; --"})

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("psycopg2", None)
        else:
            sys.modules["psycopg2"] = self._saved

    def test_idempotency_rules(self):
        self.assertEqual(self.store.add(self.event)[0], "created")
        self.assertEqual(self.store.add(dict(self.event))[0], "duplicate")
        self.assertEqual(self.store.add(dict(self.event, note="different"))[0], "conflict")
        self.assertEqual(len(self.db.rows), 1)

    def test_sql_is_parameterized_and_primary_key_decides(self):
        self.store.add(self.event)
        self.store.add(self.event)
        self.store.list()
        self.store.get("g06-m01-0201")
        inserts = [s for s, _ in self.db.log if s.lstrip().startswith("INSERT")]
        self.assertTrue(all("ON CONFLICT (event_id) DO NOTHING" in s for s in inserts))
        for sql, params in self.db.log:
            self.assertNotIn("DROP TABLE", sql)
            self.assertNotIn("g06-m01", sql)
            self.assertIsInstance(params, tuple)
        # 寫入不先查：第一個 SQL（建表之後）就是 INSERT
        statements = [s.split()[0] for s, _ in self.db.log]
        self.assertEqual(statements[:2], ["CREATE", "INSERT"])

    def test_database_errors_do_not_leak(self):
        self.db.down = True
        with self.assertRaises(service.Reject) as caught:
            self.store.add(self.event)
        rej = caught.exception
        self.assertEqual((rej.status, rej.field), (503, "database"))
        self.assertNotIn(PASSWORD, rej.error)
        self.assertNotIn("db.example", rej.error)

    def test_store_selection_and_health_flag(self):
        env = {"DB_HOST": "h", "DB_NAME": "n", "DB_USER": "u", "DB_PASSWORD": "p"}
        self.assertIs(service.PgStore(env).configured, True)
        self.assertIs(service.MemoryStore().configured, False)


if __name__ == "__main__":
    unittest.main()
