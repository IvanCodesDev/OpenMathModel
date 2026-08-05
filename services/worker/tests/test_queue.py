import os
import time

from omm_worker import FileJobQueue


def test_enqueue_claim_complete_lifecycle(tmp_path):
    queue = FileJobQueue(tmp_path)
    queue.enqueue("run_1")

    job = queue.claim()
    assert job is not None
    assert job.run_id == "run_1"
    assert job.kind == "advance"
    assert job.deliveries == 1

    assert queue.claim() is None  # nothing else pending

    queue.complete(job)
    assert queue.counts() == {"pending": 0, "claimed": 0, "done": 1, "dead": 0}


def test_single_job_cannot_be_claimed_twice(tmp_path):
    queue_a = FileJobQueue(tmp_path)
    queue_b = FileJobQueue(tmp_path)
    queue_a.enqueue("run_1")

    first = queue_a.claim()
    second = queue_b.claim()

    assert first is not None
    assert second is None


def test_fail_requeues_and_counts_deliveries(tmp_path):
    queue = FileJobQueue(tmp_path, max_deliveries=3)
    queue.enqueue("run_1")

    job = queue.claim()
    assert queue.fail(job) == "requeued"

    job = queue.claim()
    assert job.deliveries == 2


def test_poison_job_parks_in_dead(tmp_path):
    queue = FileJobQueue(tmp_path, max_deliveries=2)
    queue.enqueue("run_1")

    first = queue.claim()
    assert queue.fail(first) == "requeued"
    second = queue.claim()
    assert second.deliveries == 2
    assert queue.fail(second) == "dead"

    assert queue.claim() is None
    assert queue.counts()["dead"] == 1


def test_requeue_stale_recovers_abandoned_claims(tmp_path):
    queue = FileJobQueue(tmp_path, claim_ttl_s=60.0)
    queue.enqueue("run_1")
    job = queue.claim()
    assert queue.claim() is None

    # Backdate the claim to simulate a worker that died mid-job.
    claimed_file = tmp_path / "claimed" / f"{job.job_id}.json"
    stale = time.time() - 3600
    os.utime(claimed_file, (stale, stale))

    assert queue.requeue_stale() == 1
    recovered = queue.claim()
    assert recovered is not None
    assert recovered.job_id == job.job_id
    assert recovered.deliveries == 2


def test_job_key_is_stable_identity(tmp_path):
    queue = FileJobQueue(tmp_path)
    job_a = queue.enqueue("run_1", payload={"x": 1})
    job_b = queue.enqueue("run_1", payload={"x": 1})
    job_c = queue.enqueue("run_1", payload={"x": 2})
    assert job_a.job_key() == job_b.job_key()
    assert job_a.job_key() != job_c.job_key()
