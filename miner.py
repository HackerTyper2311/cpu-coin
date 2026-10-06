#!/usr/bin/env python3

import argparse
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_SERVER = "http://127.0.0.1:8080"
DEFAULT_WORKERS = 1
REQUEST_TIMEOUT = 15
JOB_RETRY_DELAY = 2
CHECK_INTERVAL = 4096
stop_event = threading.Event()
found_event = threading.Event()
state_lock = threading.Lock()

def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def http_get(url):
    request = Request(url, headers={"User-Agent": "CPU-Coin-Miner/1.0"})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))

def http_post(url, payload):
    body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "CPU-Coin-Miner/1.0"}, method="POST")
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}

def calculate_pow(header, nonce, n, r, p, dklen):
    password = canonical_json(header).encode("utf-8")
    salt = int(nonce).to_bytes(8, "little", signed=False)
    return hashlib.scrypt(password=password, salt=salt, n=n, r=r, p=p, dklen=dklen)

def calculate_block_hash(header, nonce, pow_hex):
    block_data = {"header": header, "nonce": nonce, "pow": pow_hex}
    return hashlib.sha256(canonical_json(block_data).encode("utf-8")).hexdigest()

def target_from_difficulty(difficulty):
    difficulty = int(difficulty)
    if difficulty <= 0:
        return (1 << 256) - 1
    if difficulty >= 64:
        return 1
    return 1 << (256 - difficulty * 4)

def meets_target(pow_bytes, target):
    return int.from_bytes(pow_bytes, "big") < target

def get_job(server, wallet_id):
    return http_get(server.rstrip("/") + "/job?" + urlencode({"id": wallet_id}))

def get_status(server):
    return http_get(server.rstrip("/") + "/api/status")

def mining_worker(worker_id, workers, job, result_container):
    header = job["header"]
    difficulty = int(job.get("difficulty", header["difficulty"]))
    target = target_from_difficulty(difficulty)
    cfg = job.get("scrypt", {})
    n = int(cfg.get("n", 4096))
    r = int(cfg.get("r", 8))
    p = int(cfg.get("p", 1))
    dklen = int(cfg.get("dklen", 32))
    nonce = worker_id
    while not stop_event.is_set() and not found_event.is_set():
        pow_bytes = calculate_pow(header, nonce, n, r, p, dklen)
        if meets_target(pow_bytes, target):
            pow_hex = pow_bytes.hex()
            with state_lock:
                result_container["found"] = {
                    "header": header,
                    "nonce": nonce,
                    "pow": pow_hex,
                    "hash": calculate_block_hash(header, nonce, pow_hex),
                }
            found_event.set()
            return
        nonce += workers

def mine_job(job, workers):
    stop_event.clear()
    found_event.clear()
    result_container = {"found": None, "error": None}
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cpu-miner") as executor:
        futures = [executor.submit(mining_worker, i, workers, job, result_container) for i in range(workers)]
        while not result_container["found"] and not result_container["error"]:
            time.sleep(0.1)
        stop_event.set()
        for future in futures:
            try:
                future.result()
            except Exception as e:
                if not result_container["error"]:
                    result_container["error"] = str(e)
    return result_container, time.time() - start

def print_banner(server, wallet_id, workers):
    print("\n======================================")
    print("          CPU COIN MINER")
    print("======================================\n")
    print(f"Server : {server}")
    print(f"Wallet : {wallet_id}")
    print(f"CPU workers: {workers}")
    print("GPU: not used\n")
    print("Algorithm: scrypt\n")

def main():
    parser = argparse.ArgumentParser(description="CPU Coin CPU miner")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="CPU Coin server URL")
    parser.add_argument("--id", required=True, help="8-character mining ID")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Number of CPU workers")
    args = parser.parse_args()
    server, wallet_id, workers = args.server.rstrip("/"), args.id, args.workers
    if len(wallet_id) != 8 or any(ord(c) < 32 or ord(c) > 126 for c in wallet_id):
        print("ERROR: wallet ID must contain exactly 8 printable ASCII characters.")
        sys.exit(1)
    if workers < 1:
        print("ERROR: workers must be at least 1.")
        sys.exit(1)
    print_banner(server, wallet_id, workers)
    print("Connecting to server...")
    try:
        status = get_status(server)
    except URLError as e:
        print(f"Connection failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)
    if not status.get("ok"):
        print("Server returned an error:", status.get("error", "unknown error"))
        sys.exit(1)
    print(f"Server online | height {status.get('height', '?')} | difficulty {status.get('difficulty', '?')} | reward {status.get('reward', '?')} CPUC\n")
    while True:
        try:
            job = get_job(server, wallet_id)
        except KeyboardInterrupt:
            print("\nStopping miner...")
            return
        except Exception as e:
            print(f"Could not get mining job: {e}")
            time.sleep(JOB_RETRY_DELAY)
            continue
        if not job.get("ok"):
            print("Server rejected job request:", job.get("error", "unknown error"))
            time.sleep(JOB_RETRY_DELAY)
            continue
        header = job.get("header")
        if not isinstance(header, dict):
            print("Server returned an invalid job.")
            time.sleep(JOB_RETRY_DELAY)
            continue
        height = header.get("index", job.get("height", "?"))
        print(f"Mining height {height} for {wallet_id}...", flush=True)
        result, elapsed = mine_job(job, workers)
        if result["error"]:
            print("Mining error:", result["error"])
            time.sleep(1)
            continue
        block = result["found"]
        if block is None:
            print("No block found.")
            continue
        print(f"Found nonce: {block['nonce']}")
        print(f"PoW: {block['pow']}")
        print(f"Block hash: {block['hash']}")
        print("Submitting block...")
        try:
            response = http_post(server + "/submit", block)
        except Exception as e:
            print(f"Submission failed: {e}")
            time.sleep(1)
            continue
        if response.get("ok"):
            print(f"Accepted block #{response.get('height', height)}!")
            print(f"Reward: +{response.get('reward', job.get('reward', 0))} CPUC")
            print(f"Balance: {response.get('balance', '?')} CPUC\n")
        else:
            print("Rejected:", json.dumps(response, ensure_ascii=False))
            print()
        time.sleep(0.1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nMiner stopped.")
    except Exception as e:
        print(f"\nFatal error: {e}")
        sys.exit(1)
