#!/usr/bin/env python3

import json
import time
import hashlib
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from html import escape

HOST = "0.0.0.0"
PORT = 8080
CHAIN_FILE = Path("blockchain.json")
WALLETS_FILE = Path("wallets.json")
COIN_NAME = "CPU Coin"
SYMBOL = "CPUC"
BLOCK_REWARD = 50.0
DIFFICULTY = 3
SCRYPT_N = 4096
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
MAX_CLOCK_SKEW = 15 * 60

state_lock = threading.RLock()
blockchain = []
wallets = {}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_json(path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARNING] Could not load {path}: {e}")
        return default


def atomic_save_json(path, data):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    temp_path.replace(path)


def save_blockchain():
    atomic_save_json(CHAIN_FILE, blockchain)


def save_wallets():
    atomic_save_json(WALLETS_FILE, wallets)


def valid_wallet_id(wallet_id):
    if not isinstance(wallet_id, str) or len(wallet_id) != 8:
        return False
    return all(32 <= ord(c) <= 126 for c in wallet_id)


def get_or_create_wallet(wallet_id):
    if not valid_wallet_id(wallet_id):
        raise ValueError("Wallet ID must contain exactly 8 printable ASCII characters")
    with state_lock:
        if wallet_id not in wallets:
            wallets[wallet_id] = {
                "id": wallet_id,
                "balance": 0.0,
                "blocks_mined": 0,
                "created": int(time.time()),
            }
            save_wallets()
        return wallets[wallet_id]


def calculate_pow(header, nonce):
    password = canonical_json(header).encode("utf-8")
    salt = int(nonce).to_bytes(8, "little", signed=False)
    return hashlib.scrypt(
        password=password,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def calculate_block_hash(block):
    block_data = {
        "header": block["header"],
        "nonce": block["nonce"],
        "pow": block["pow"],
    }
    return hashlib.sha256(canonical_json(block_data).encode("utf-8")).hexdigest()


def target_for_difficulty(difficulty):
    difficulty = int(difficulty)
    if difficulty <= 0:
        return (1 << 256) - 1
    if difficulty >= 64:
        return 1
    return 1 << (256 - difficulty * 4)


def pow_meets_target(pow_hex, difficulty):
    try:
        raw = bytes.fromhex(pow_hex)
    except Exception:
        return False
    if len(raw) != 32:
        return False
    return int.from_bytes(raw, "big") < target_for_difficulty(difficulty)


def create_genesis_block():
    header = {
        "index": 0,
        "previous_hash": "0" * 64,
        "timestamp": int(time.time()),
        "difficulty": DIFFICULTY,
        "reward": 0.0,
        "miner_id": "GENESIS",
    }
    block = {"header": header, "nonce": 0, "pow": "00" * 32}
    block["hash"] = calculate_block_hash(block)
    return block


def load_state():
    global blockchain, wallets
    loaded_chain = load_json(CHAIN_FILE, [])
    loaded_wallets = load_json(WALLETS_FILE, {})
    blockchain = loaded_chain if isinstance(loaded_chain, list) else []
    wallets = loaded_wallets if isinstance(loaded_wallets, dict) else {}
    if not blockchain:
        blockchain.append(create_genesis_block())
        save_blockchain()
    print(f"[STATE] Blockchain height: {len(blockchain) - 1}")
    print(f"[STATE] Wallets: {len(wallets)}")


def create_job(wallet_id):
    get_or_create_wallet(wallet_id)
    with state_lock:
        previous_block = blockchain[-1]
        header = {
            "index": previous_block["header"]["index"] + 1,
            "previous_hash": previous_block["hash"],
            "timestamp": int(time.time()),
            "difficulty": DIFFICULTY,
            "reward": BLOCK_REWARD,
            "miner_id": wallet_id,
        }
        target = target_for_difficulty(DIFFICULTY)
        return {
            "ok": True,
            "job_id": f"{header['index']}-{header['timestamp']}",
            "height": header["index"],
            "index": header["index"],
            "header": header,
            "difficulty": DIFFICULTY,
            "reward": BLOCK_REWARD,
            "target": target,
            "target_hex": f"{target:064x}",
            "algorithm": "scrypt",
            "scrypt": {"n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": SCRYPT_DKLEN},
            "miner_id": wallet_id,
        }


def validate_block(block):
    if not isinstance(block, dict):
        return False, "block must be an object"
    if not all(k in block for k in ("header", "nonce", "pow", "hash")):
        return False, "missing block fields"
    header = block["header"]
    if not isinstance(header, dict):
        return False, "header must be an object"
    required = {"index", "previous_hash", "timestamp", "difficulty", "reward", "miner_id"}
    if set(header.keys()) != required:
        return False, "invalid header fields"
    if not isinstance(header["index"], int):
        return False, "invalid index"
    if not isinstance(header["previous_hash"], str) or len(header["previous_hash"]) != 64:
        return False, "invalid previous hash"
    if not isinstance(header["timestamp"], int):
        return False, "invalid timestamp"
    if not isinstance(header["difficulty"], int):
        return False, "invalid difficulty"
    if not isinstance(header["reward"], (int, float)):
        return False, "invalid reward"
    if not isinstance(header["miner_id"], str):
        return False, "invalid miner id"

    with state_lock:
        previous_block = blockchain[-1]
        if header["index"] != previous_block["header"]["index"] + 1:
            return False, "wrong block height"
        if header["previous_hash"] != previous_block["hash"]:
            return False, "wrong previous hash"

    if header["difficulty"] != DIFFICULTY:
        return False, "wrong difficulty"
    if float(header["reward"]) != float(BLOCK_REWARD):
        return False, "wrong block reward"
    if not valid_wallet_id(header["miner_id"]):
        return False, "invalid miner id"

    now = int(time.time())
    if header["timestamp"] < now - MAX_CLOCK_SKEW:
        return False, "block timestamp too old"
    if header["timestamp"] > now + MAX_CLOCK_SKEW:
        return False, "block timestamp is in the future"

    nonce = block["nonce"]
    if not isinstance(nonce, int) or not 0 <= nonce <= 0xFFFFFFFFFFFFFFFF:
        return False, "invalid nonce"

    pow_hex = block["pow"]
    if not isinstance(pow_hex, str) or len(pow_hex) != 64:
        return False, "pow must contain 32 bytes"
    try:
        bytes.fromhex(pow_hex)
    except ValueError:
        return False, "pow is not valid hexadecimal"

    expected_pow = calculate_pow(header, nonce).hex()
    if pow_hex.lower() != expected_pow:
        return False, "invalid proof of work"
    if not pow_meets_target(pow_hex, header["difficulty"]):
        return False, "proof of work does not meet difficulty"

    expected_hash = calculate_block_hash(block)
    if str(block["hash"]).lower() != expected_hash:
        return False, "invalid block hash"
    return True, "valid"


def send_json(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if length <= 0:
            return None
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except Exception:
        return None


def dashboard_html():
    with state_lock:
        height = len(blockchain) - 1
        recent_blocks = list(reversed(blockchain[-12:]))
        wallet_count = len(wallets)

    rows = []
    for block in recent_blocks:
        header = block.get("header", {})
        rows.append(f"""
        <tr>
          <td>{escape(str(header.get('index', '?')))}</td>
          <td>{escape(str(header.get('miner_id', '?')))}</td>
          <td>{escape(str(header.get('reward', '?')))} {SYMBOL}</td>
          <td>{escape(str(block.get('nonce', '?')))}</td>
          <td><code>{escape(str(block.get('hash', ''))[:20])}...</code></td>
        </tr>
        """)
    if not rows:
        rows.append("<tr><td colspan='5'>No blocks</td></tr>")

    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{escape(COIN_NAME)} Server</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:radial-gradient(circle at top left,#182038 0,#0b1020 45%,#070b14 100%);color:#f4f7ff;min-height:100vh}}.container{{max-width:1200px;margin:auto;padding:30px 18px 60px}}.header{{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:28px}}.brand{{display:flex;align-items:center;gap:14px}}.logo{{width:54px;height:54px;border-radius:16px;display:grid;place-items:center;font-size:24px;font-weight:900;background:linear-gradient(135deg,#926ee4,#6446bc);box-shadow:0 10px 40px rgba(146,110,228,.25)}}h1{{margin:0;font-size:24px}}.subtitle{{margin-top:3px;color:#9ba6bf;font-size:14px}}.badge{{padding:8px 12px;border-radius:999px;background:rgba(57,211,83,.12);border:1px solid rgba(57,211,83,.25);color:#6ee681;font-size:13px;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.card{{background:rgba(18,25,43,.8);border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:20px;box-shadow:0 15px 50px rgba(0,0,0,.22);backdrop-filter:blur(12px)}}.label{{color:#8995b0;font-size:13px;margin-bottom:8px}}.value{{font-size:26px;font-weight:800}}.section{{margin-top:18px}}.wallet-search{{display:flex;gap:10px;flex-wrap:wrap}}input{{flex:1;min-width:180px;border:1px solid rgba(255,255,255,.1);background:#0a0f1c;color:white;border-radius:12px;padding:13px 14px;outline:none}}button{{border:0;border-radius:12px;padding:13px 18px;background:#926ee4;color:white;font-weight:800;cursor:pointer}}button:hover{{filter:brightness(1.08)}}#walletResult{{margin-top:14px;color:#c6cde0}}.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:700px}}th,td{{padding:13px 12px;border-bottom:1px solid rgba(255,255,255,.06);text-align:left}}th{{color:#8995b0;font-size:13px}}td{{color:#dce2ef;font-size:14px}}code{{color:#bda9ff}}.footer{{margin-top:26px;color:#727d96;font-size:13px}}@media(max-width:850px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}@media(max-width:520px){{.header{{align-items:flex-start;flex-direction:column}}.grid{{grid-template-columns:1fr}}}}
</style></head>
<body><div class='container'>
<div class='header'><div class='brand'><div class='logo'>₿</div><div><h1>{escape(COIN_NAME)}</h1><div class='subtitle'>CPU-only proof-of-work network</div></div></div><div class='badge'>SERVER ONLINE</div></div>
<div class='grid'>
<div class='card'><div class='label'>Blockchain height</div><div class='value'>{height}</div></div>
<div class='card'><div class='label'>Difficulty</div><div class='value'>{DIFFICULTY}</div></div>
<div class='card'><div class='label'>Block reward</div><div class='value'>{BLOCK_REWARD:g} {SYMBOL}</div></div>
<div class='card'><div class='label'>Wallets</div><div class='value'>{wallet_count}</div></div>
</div>
<div class='card section'><div class='label'>Wallet lookup</div><div class='wallet-search'><input id='walletId' maxlength='8' placeholder='8-character ID'><button onclick='lookupWallet()'>Look up</button></div><div id='walletResult'></div></div>
<div class='card section'><div class='label'>Recent blocks</div><div class='table-wrap'><table><thead><tr><th>Height</th><th>Miner</th><th>Reward</th><th>Nonce</th><th>Hash</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>
<div class='footer'>{escape(COIN_NAME)} {escape(SYMBOL)} — CPU mining only — No accounts — IDs are 8 printable ASCII characters</div>
</div>
<script>
async function lookupWallet(){{const id=document.getElementById('walletId').value;if(id.length!==8){{document.getElementById('walletResult').textContent='Wallet ID must contain exactly 8 characters.';return}}try{{const response=await fetch('/api/wallet/'+encodeURIComponent(id));const data=await response.json();if(!data.ok){{document.getElementById('walletResult').textContent=data.error||'Wallet not found.';return}}const wallet=data.wallet;document.getElementById('walletResult').innerHTML='<strong>'+wallet.id+'</strong> — '+Number(wallet.balance).toFixed(2)+' {SYMBOL} — '+wallet.blocks_mined+' blocks mined'}}catch(error){{document.getElementById('walletResult').textContent='Could not contact server.'}}}}
</script></body></html>"""


class CPUCoinHandler(BaseHTTPRequestHandler):
    server_version = "CPU-Coin/1.0"

    def log_message(self, fmt, *args):
        print("[HTTP] " + fmt % args)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            body = dashboard_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/status", "/api/status"):
            with state_lock:
                height = len(blockchain) - 1
                wallet_count = len(wallets)
            send_json(self, {
                "ok": True,
                "name": COIN_NAME,
                "symbol": SYMBOL,
                "height": height,
                "chain_height": height,
                "difficulty": DIFFICULTY,
                "reward": BLOCK_REWARD,
                "block_reward": BLOCK_REWARD,
                "wallets": wallet_count,
                "wallet_count": wallet_count,
                "mining": False,
                "server_mining": False,
                "algorithm": "scrypt",
                "scrypt": {"n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": SCRYPT_DKLEN},
            })
            return

        if path in ("/job", "/api/job"):
            wallet_id = query.get("id", [""])[0]
            if not wallet_id:
                send_json(self, {"ok": False, "error": "missing id"}, 400)
                return
            if not valid_wallet_id(wallet_id):
                send_json(self, {"ok": False, "error": "id must contain exactly 8 printable ASCII characters"}, 400)
                return
            try:
                send_json(self, create_job(wallet_id))
            except Exception as e:
                send_json(self, {"ok": False, "error": str(e)}, 500)
            return

        if path in ("/chain", "/api/chain"):
            with state_lock:
                chain_copy = list(blockchain)
            send_json(self, {"ok": True, "height": len(chain_copy) - 1, "chain": chain_copy})
            return

        if path in ("/wallets", "/api/wallets"):
            with state_lock:
                wallet_copy = dict(wallets)
            send_json(self, {"ok": True, "wallets": wallet_copy})
            return

        wallet_id = None
        for prefix in ("/wallet/", "/api/wallet/"):
            if path.startswith(prefix):
                wallet_id = path[len(prefix):].strip()
                break
        if wallet_id is not None:
            if not valid_wallet_id(wallet_id):
                send_json(self, {"ok": False, "error": "invalid wallet id"}, 400)
                return
            with state_lock:
                wallet = wallets.get(wallet_id)
            if wallet is None:
                send_json(self, {"ok": False, "error": "wallet not found"}, 404)
                return
            send_json(self, {"ok": True, "wallet": wallet})
            return

        send_json(self, {"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/submit", "/api/submit"):
            data = read_json_body(self)
            if data is None:
                send_json(self, {"ok": False, "error": "invalid JSON"}, 400)
                return
            block = data.get("block") if isinstance(data, dict) and isinstance(data.get("block"), dict) else data
            with state_lock:
                valid, reason = validate_block(block)
                if not valid:
                    send_json(self, {"ok": False, "error": reason}, 400)
                    return
                wallet_id = block["header"]["miner_id"]
                wallet = get_or_create_wallet(wallet_id)
                reward = float(block["header"]["reward"])
                wallet["balance"] = float(wallet.get("balance", 0.0)) + reward
                wallet["blocks_mined"] = int(wallet.get("blocks_mined", 0)) + 1
                accepted_block = {
                    "header": dict(block["header"]),
                    "nonce": int(block["nonce"]),
                    "pow": str(block["pow"]).lower(),
                    "hash": str(block["hash"]).lower(),
                }
                blockchain.append(accepted_block)
                save_blockchain()
                save_wallets()
                new_height = accepted_block["header"]["index"]
                new_balance = wallet["balance"]
            print(f"[BLOCK] Accepted #{new_height} from {wallet_id} +{reward:g} {SYMBOL}")
            send_json(self, {
                "ok": True,
                "accepted": True,
                "height": new_height,
                "block_height": new_height,
                "hash": accepted_block["hash"],
                "miner_id": wallet_id,
                "reward": reward,
                "balance": new_balance,
                "message": f"Block {new_height} accepted. {reward:g} {SYMBOL} credited.",
            })
            return
        send_json(self, {"ok": False, "error": "not found"}, 404)


def main():
    print("\n======================================")
    print("          CPU COIN SERVER")
    print("======================================\n")
    load_state()
    print(f"\nListening on http://{HOST}:{PORT}")
    print("Mining: DISABLED")
    print(f"Difficulty: {DIFFICULTY}")
    print(f"Reward: {BLOCK_REWARD} {SYMBOL}")
    print("Algorithm: scrypt\n")
    server = ThreadingHTTPServer((HOST, PORT), CPUCoinHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
