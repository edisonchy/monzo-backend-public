from unittest.mock import Mock

from sqlalchemy.exc import OperationalError

from monzo_backend import worker


def test_cleanup_failure_does_not_stop_jobs_and_retries_next_minute(monkeypatch, caplog):
    elapsed, attempts, jobs = [0], [], []
    monkeypatch.setattr(worker, "stopping", False)
    monkeypatch.setattr(worker.time, "monotonic", lambda: elapsed[0])

    def cleanup(s):
        attempts.append(elapsed[0])
        if len(attempts) == 1:
            raise OperationalError("private statement", {}, Exception("private detail"))

    def tick(queue):
        jobs.append(queue)
        elapsed[0] += 30
        if elapsed[0] >= 120:
            worker.stopping = True
        return True

    monkeypatch.setattr(worker, "expire_previews", cleanup)
    monkeypatch.setattr(worker, "tick", tick)
    worker.run("category", Mock())
    assert attempts == [0, 60]
    assert jobs == ["category"] * 4
    assert "cleanup failed: type=OperationalError" in caplog.text
    assert "private" not in caplog.text


def test_sync_worker_does_not_run_category_maintenance(monkeypatch):
    cleanup = Mock()
    monkeypatch.setattr(worker, "stopping", False)
    monkeypatch.setattr(worker, "expire_previews", cleanup)

    def tick(queue):
        assert queue == "sync"
        worker.stopping = True
        return True

    monkeypatch.setattr(worker, "tick", tick)
    worker.run("sync", Mock())
    cleanup.assert_not_called()
