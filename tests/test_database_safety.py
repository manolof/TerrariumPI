"""SQLite durability on a Pi that browns out (audit 2026-11-12, core I3, API D3)."""
import sqlite3

import terrariumDatabase


def test_writes_are_synchronous_normal_by_default():
    source = open(terrariumDatabase.__file__, encoding="utf-8").read()
    assert '"synchronous": "NORMAL"' in source and '"synchronous": "OFF"' not in source


def test_a_sound_database_passes_the_check(tmp_path):
    path = tmp_path / "good.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (x INTEGER)")
        connection.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    assert terrariumDatabase.integrity_problem(str(path)) is None


def test_a_damaged_database_is_reported(tmp_path):
    path = tmp_path / "bad.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (x TEXT)")
        connection.executemany("INSERT INTO t VALUES (?)", [("x" * 500,) for _ in range(200)])
    data = bytearray(path.read_bytes())
    for offset in range(4096, min(len(data), 12288)):
        data[offset] = 0xFF
    path.write_bytes(bytes(data))
    problem = terrariumDatabase.integrity_problem(str(path))
    assert problem is not None and problem != "ok"


def test_a_missing_file_is_not_a_problem(tmp_path):
    assert terrariumDatabase.integrity_problem(str(tmp_path / "none.db")) is None


def test_init_runs_the_check_and_keeps_the_result():
    source = open(terrariumDatabase.__file__, encoding="utf-8").read()
    init = source[source.index("def init(") : source.index("def create_defaults")]
    assert "integrity_problem(DATABASE)" in init and "INTEGRITY_PROBLEM" in init


def test_the_engine_mails_a_failed_check_after_the_outbox_starts():
    source = open(terrariumDatabase.__file__.replace("terrariumDatabase.py", "terrariumEngine.py"), encoding="utf-8").read()
    assert source.index("terrariumDatabase.INTEGRITY_PROBLEM") > source.index("self.notification.start_outbox()")
    assert '"db:quick_check"' in source
