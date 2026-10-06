CPU Coin
========

Files:
- cookie-miner.py  CPU Coin server (does not mine)
- miner.py         CPU-only miner

Server:
  python3 cookie-miner.py

Miner:
  python3 miner.py --server http://100.120.173.15:8080 --id quickfix --workers 6

The server creates blockchain.json and wallets.json automatically.

The mining ID must be exactly 8 printable ASCII characters.
