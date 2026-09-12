"""Locking behaviour.

`filesystem_lock` is re-entrant within a thread so that publication, backup, and
repair can nest it, but it must stay exclusive against other processes. These
tests pin both halves: nesting must not block, and a separate process must.
"""

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from termicast.database import filesystem_lock
from termicast.publisher import operation_lock, output_lock


@pytest.fixture
def time_box():
    """Turn a deadlock into a failure instead of a hung suite."""
    def bail(*args):
        raise TimeoutError("filesystem_lock blocked on a nested acquisition")

    previous = signal.signal(signal.SIGALRM, bail)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


PROBE = """
import fcntl, sys, time
handle = open(sys.argv[1], "a+b")
start = time.time()
fcntl.flock(handle, fcntl.LOCK_EX)
print(round(time.time() - start, 2))
"""


def _probe(path):
    """A separate process that blocks until it can take the lock, then reports."""
    return subprocess.Popen([sys.executable, "-c", PROBE, str(path)],
                            stdout=subprocess.PIPE, text=True)


def test_nested_acquisition_does_not_block(db, time_box):
    with db.lock():
        with db.lock():
            with db.lock():
                pass


def test_depth_unwinds_so_the_lock_is_released(db, time_box, tmp_path):
    with db.lock():
        with db.lock():
            pass
    child = _probe(str(db.path) + ".lock")
    assert float(child.communicate(timeout=10)[0]) < 1.0


def test_an_exception_inside_a_nested_block_still_releases(db, time_box):
    with pytest.raises(RuntimeError):
        with db.lock():
            with db.lock():
                raise RuntimeError("boom")
    child = _probe(str(db.path) + ".lock")
    assert float(child.communicate(timeout=10)[0]) < 1.0


def test_another_process_still_blocks_while_held(db, tmp_path):
    """Re-entrance must not weaken cross-process exclusion."""
    with db.lock():
        child = _probe(str(db.path) + ".lock")
        time.sleep(1.5)
        assert child.poll() is None, "another process acquired a held lock"
    waited = float(child.communicate(timeout=10)[0])
    assert waited >= 1.4, f"child did not actually wait (waited {waited}s)"


def test_another_thread_still_blocks_while_held(db):
    """Depth is thread-local: a second thread must take the real flock."""
    acquired, release = threading.Event(), threading.Event()

    def take_it():
        with db.lock():
            acquired.set()
            release.wait(10)

    thread = threading.Thread(target=take_it, daemon=True)
    with db.lock():
        thread.start()
        assert not acquired.wait(1.5), "a second thread reused another thread's lock"
    # Released here, so the thread may now proceed.
    assert acquired.wait(10), "the second thread never acquired the released lock"
    release.set()
    thread.join(10)
    assert not thread.is_alive()


def test_distinct_lock_paths_are_independent(db, show, time_box):
    # Callers create the output directory before locking in it; publisher._write
    # and repair_show both mkdir first.
    Path(show["output_dir"]).mkdir(parents=True, exist_ok=True)
    with db.lock(), operation_lock(show), output_lock(show):
        pass


def test_lock_paths_are_normalized(db, show, time_box):
    """Two spellings of one path must be recognised as the same lock."""
    output = Path(show["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    spelled = dict(show, output_dir=str(output.parent / "." / output.name))
    with output_lock(show), output_lock(spelled):
        pass


def test_sqlite_connection_takes_no_file_lock(db, time_box):
    """Lock-free readers are what let helpers run inside db.lock()."""
    with db.lock():
        with db.connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert db.list_shows() == db.list_shows()


def test_backup_works_while_the_database_lock_is_held(db, show, time_box):
    """The B key is offered on every menu, including prompts shown inside a lock.

    `Publisher.merge` runs its caller-supplied callback while holding the
    database lock, and the CSV import path shows a confirmation there. Pressing
    B reaches `create_backup` without `_locked`, which used to deadlock.
    """
    from termicast.backup import create_backup
    Path(show["output_dir"]).mkdir(parents=True, exist_ok=True)
    with db.lock():
        backup = create_backup(db)
    assert Path(backup).is_file()


def test_merge_callback_may_take_a_backup(db, show, time_box):
    """The same path end to end: a prompt inside merge's lock taking a backup."""
    from termicast.backup import create_backup
    from termicast.models import new_episode
    from termicast.publisher import Publisher
    Path(show["output_dir"]).mkdir(parents=True, exist_ok=True)
    taken = []

    def callback(existing, current_show):
        taken.append(create_backup(db))          # what pressing B does
        return [new_episode(guid="ep-1", title="One", description="d", slug="ep1",
                            mp3_url="https://example.org/show/audio/ep1.mp3",
                            length=5, duration=60.0)]

    Publisher(db).merge(show["id"], callback)
    assert taken and Path(taken[0]).is_file()
    assert [e["guid"] for e in db.list_episodes(show["id"])] == ["ep-1"]
