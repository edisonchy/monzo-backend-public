import argparse
import logging
import signal
import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from monzo_backend.category_operations import expire_previews
from monzo_backend.classifier import classify
from monzo_backend.db import engine, session
from monzo_backend.domain import LostSyncLease, claim, finish
from monzo_backend.http import ServiceError
from monzo_backend.monzo import execute

log = logging.getLogger("monzo.worker")
stopping = False


def stop(*_):
    global stopping
    stopping = True


def tick(queue):
    with session() as s, s.begin():
        work = claim(s, queue)
    if not work:
        return False
    result, error = None, None
    try:
        if queue == "category":
            result = classify(work)
        else:
            execute(work)
            return True  # Sync execution committed its result and completion together.
    except LostSyncLease:
        return True
    except ServiceError as exc:
        error = exc
    except Exception as exc:
        # Do not emit exception strings or locals: they may contain bank data.
        log.error("Job failed: type=%s job=%s", type(exc).__name__, work["id"])
        error = ServiceError("internal_processing_error", False)
    with session() as s, s.begin():
        finish(s, work, result=result, error=error)
    return True


def run(queue, lock):
    next_cleanup = 0.0
    while not stopping:
        lock.execute(text("SELECT 1"))
        if queue == "category" and time.monotonic() >= next_cleanup:
            try:
                with session() as s, s.begin():
                    expire_previews(s)
            except SQLAlchemyError as exc:
                # Roll back and retry next minute without delaying category jobs.
                log.warning("Category preview cleanup failed: type=%s", type(exc).__name__)
            next_cleanup = time.monotonic() + 60
        if not tick(queue):
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("queue", choices=["sync", "category"])
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.basicConfig(level=logging.INFO)
    # Session advisory lock prevents duplicate consumers during rolling deployment.
    # Dedicated connection must remain healthy; exit if it is lost.
    with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        key = 712001 if args.queue == "sync" else 712002
        if not lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
            raise SystemExit("A worker for this queue is already running")
        run(args.queue, lock)


if __name__ == "__main__":
    main()
