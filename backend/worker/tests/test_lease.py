import time

from omm_worker import RunLeaseStore


def test_acquire_is_exclusive_until_release(tmp_path):
    store = RunLeaseStore(tmp_path, ttl_s=60)

    lease = store.acquire("run_1", "worker_a")
    assert lease is not None
    assert store.acquire("run_1", "worker_b") is None

    store.release(lease)
    retaken = store.acquire("run_1", "worker_b")
    assert retaken is not None
    assert retaken.owner == "worker_b"


def test_leases_are_per_run(tmp_path):
    store = RunLeaseStore(tmp_path, ttl_s=60)
    assert store.acquire("run_1", "worker_a") is not None
    assert store.acquire("run_2", "worker_a") is not None


def test_expired_lease_can_be_stolen_and_old_renew_fails(tmp_path):
    store = RunLeaseStore(tmp_path, ttl_s=0.05)
    original = store.acquire("run_1", "worker_a")
    assert original is not None

    time.sleep(0.1)
    stolen = store.acquire("run_1", "worker_b")
    assert stolen is not None
    assert stolen.owner == "worker_b"

    # The zombie holder must notice it lost the lease.
    assert store.renew(original) is None


def test_renew_extends_a_held_lease(tmp_path):
    store = RunLeaseStore(tmp_path, ttl_s=0.3)
    lease = store.acquire("run_1", "worker_a")
    time.sleep(0.15)
    renewed = store.renew(lease)
    assert renewed is not None
    time.sleep(0.2)  # beyond the original expiry, within the renewed one
    assert store.acquire("run_1", "worker_b") is None


def test_release_with_stale_token_is_a_noop(tmp_path):
    store = RunLeaseStore(tmp_path, ttl_s=0.05)
    original = store.acquire("run_1", "worker_a")
    time.sleep(0.1)
    stolen = store.acquire("run_1", "worker_b")
    assert stolen is not None

    store.release(original)  # stale token: must NOT delete worker_b's lease
    assert store.acquire("run_1", "worker_c") is None
