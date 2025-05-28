import hashlib
import json
import os
import socket
import threading
import time
import argparse
from collections import defaultdict
from uuid import uuid4
import requests

# ----- CONFIGURATION -----
MAX_SUPPLY = 21_000_000
BLOCK_REWARD = 6.25
DIFFICULTY = 4        # number of leading zeros
P2P_PORT = 5000       # default port for P2P communication (TCP)
STORAGE_FILE = 'chain_state.json'
DISCOVERY_PORT = 5001  # port for UDP peer discovery (optional)
BOOTSTRAP_URL = None    # URL to JSON list of bootstrap nodes
TRACKER_URL = None      # URL of tracker API for peer registration
DISCOVERY_INTERVAL = 10  # seconds

# ----- BLOCKCHAIN CORE -----
class Block:
    def __init__(self, index, prev_hash, transactions, timestamp=None, nonce=0):
        self.index = index
        self.prev_hash = prev_hash
        self.timestamp = timestamp or time.time()
        self.transactions = transactions
        self.nonce = nonce
        self.hash = self.compute_hash()

    def compute_hash(self):
        data = json.dumps({
            'index': self.index,
            'prev_hash': self.prev_hash,
            'timestamp': self.timestamp,
            'transactions': self.transactions,
            'nonce': self.nonce
        }, sort_keys=True).encode()
        return hashlib.sha256(data).hexdigest()

    def mine(self, difficulty):
        target = '0' * difficulty
        while not self.hash.startswith(target):
            self.nonce += 1
            self.hash = self.compute_hash()
        return self.hash

class Blockchain:
    def __init__(self):
        self.chain = []
        self.pending_tx = []
        self.balances = defaultdict(float)
        self.total_minted = 0.0
        self.peers = set()  # set of (ip, port)
        self.load_state()

    def create_genesis(self):
        genesis = Block(0, '0'*64, [])
        self.chain = [genesis]
        self.save_state()

    def add_transaction(self, sender, recipient, amount, broadcast=True):
        if self.balances[sender] < amount:
            raise ValueError('Insufficient balance')
        tx = {'sender': sender, 'recipient': recipient, 'amount': amount}
        self.pending_tx.append(tx)
        self.save_state()
        if broadcast:
            self.broadcast({'type': 'NEW_TX', 'data': tx})

    def mine_pending(self, miner_address, broadcast=True):
        last_hash = self.chain[-1].hash
        block = Block(len(self.chain), last_hash, list(self.pending_tx))
        block.mine(DIFFICULTY)
        self.chain.append(block)
        reward = min(BLOCK_REWARD, MAX_SUPPLY - self.total_minted)
        self.balances[miner_address] += reward
        self.total_minted += reward
        for tx in self.pending_tx:
            self.balances[tx['sender']] -= tx['amount']
            self.balances[tx['recipient']] += tx['amount']
        self.pending_tx = []
        self.save_state()
        if broadcast:
            self.broadcast({'type': 'NEW_BLOCK', 'data': vars(block)})
        return block.hash

    def is_valid_block(self, blk):
        block = Block(blk['index'], blk['prev_hash'], blk['transactions'],
                      timestamp=blk['timestamp'], nonce=blk['nonce'])
        return block.hash == blk['hash'] and block.prev_hash == self.chain[-1].hash

    # ----- NETWORK -----
    def broadcast(self, message):
        payload = json.dumps(message).encode()
        for host, port in list(self.peers):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect((host, port))
                s.sendall(payload)
                s.close()
            except:
                continue

    def serve(self, host, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen()
        print(f'Serving P2P on {host}:{port}')

        def handler(conn, _):
            data = conn.recv(10_000_000)
            conn.close()
            try:
                msg = json.loads(data.decode())
            except:
                return
            typ = msg.get('type')
            d = msg.get('data')
            if typ == 'NEW_TX':
                if d not in self.pending_tx:
                    self.add_transaction(d['sender'], d['recipient'], d['amount'], broadcast=False)
            elif typ == 'NEW_BLOCK':
                if d['index'] == len(self.chain) and self.is_valid_block(d):
                    blk = Block(d['index'], d['prev_hash'], d['transactions'],
                                timestamp=d['timestamp'], nonce=d['nonce'])
                    blk.hash = d['hash']
                    self.chain.append(blk)
                    for tx in d['transactions']:
                        self.balances[tx['sender']] -= tx['amount']
                        self.balances[tx['recipient']] += tx['amount']
                    self.total_minted += min(BLOCK_REWARD, MAX_SUPPLY - self.total_minted)
                    self.pending_tx = []
                    self.save_state()

        threading.Thread(target=self._accept, args=(s, handler), daemon=True).start()

    def _accept(self, sock, handler):
        while True:
            conn, addr = sock.accept()
            threading.Thread(target=handler, args=(conn, addr), daemon=True).start()

    # ----- BOOTSTRAP & TRACKER -----
    def load_bootstrap(self, url):
        try:
            nodes = requests.get(url, timeout=5).json()
            for n in nodes:
                self.peers.add((n['host'], n['port']))
        except Exception as e:
            print('Bootstrap load failed:', e)

    def register_tracker(self, url, public_host, public_port):
        try:
            requests.post(url + '/register', json={'host': public_host, 'port': public_port}, timeout=5)
        except Exception as e:
            print('Tracker register failed:', e)

    def fetch_tracker(self, url):
        try:
            peers = requests.get(url + '/peers', timeout=5).json()
            for p in peers:
                self.peers.add((p['host'], p['port']))
        except Exception as e:
            print('Tracker fetch failed:', e)

    # ----- Persistence -----
    def save_state(self):
        state = {'chain': [vars(b) for b in self.chain],
                 'pending_tx': self.pending_tx,
                 'balances': dict(self.balances),
                 'total_minted': self.total_minted,
                 'peers': list(self.peers)}
        with open(STORAGE_FILE, 'w') as f:
            json.dump(state, f, indent=2)

    def load_state(self):
        if not os.path.exists(STORAGE_FILE):
            self.create_genesis()
        else:
            data = json.load(open(STORAGE_FILE))
            self.chain = []
            for b in data['chain']:
                blk = Block(b['index'], b['prev_hash'], b['transactions'],
                            timestamp=b['timestamp'], nonce=b['nonce'])
                blk.hash = b['hash']
                self.chain.append(blk)
            self.pending_tx = data.get('pending_tx', [])
            self.balances = defaultdict(float, data.get('balances', {}))
            self.total_minted = data.get('total_minted', 0.0)
            self.peers = set(tuple(x) for x in data.get('peers', []))

# ----- CLI & MAIN -----
def main():
    parser = argparse.ArgumentParser(description='Blockchain Node with Bootstrap/Tracker')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind')
    parser.add_argument('--wallet', help='Your wallet address')
    parser.add_argument('--bootstrap', help='URL to bootstrap JSON list')
    parser.add_argument('--tracker', help='Base URL of tracker API')
    args = parser.parse_args()

    global BOOTSTRAP_URL, TRACKER_URL
    BOOTSTRAP_URL = args.bootstrap
    TRACKER_URL = args.tracker

    node = Blockchain()
    wallet = args.wallet or str(uuid4())
    print('Wallet:', wallet)

    # Bootstrap peers
    if BOOTSTRAP_URL:
        node.load_bootstrap(BOOTSTRAP_URL)
    # Tracker register & fetch
    if TRACKER_URL:
        public_ip = requests.get('https://ifconfig.me', timeout=5).text
        node.register_tracker(TRACKER_URL, public_ip, P2P_PORT)
        node.fetch_tracker(TRACKER_URL)

    # Start P2P server
    node.serve(args.host, P2P_PORT)

    # CLI loop
    print('Commands: tx <to> <amount>, mine, balance, stats, peers, exit')
    while True:
        cmd = input('> ').split()
        try:
            if not cmd:
                continue
            if cmd[0] == 'tx' and len(cmd) == 3:
                node.add_transaction(wallet, cmd[1], float(cmd[2]))
                print('Transaction broadcasted')
            elif cmd[0] == 'mine':
                h = node.mine_pending(wallet)
                print('Mined:', h)
            elif cmd[0] == 'balance':
                print('Balance:', node.balances[wallet])
            elif cmd[0] == 'stats':
                print('Blocks:', len(node.chain), 'Circulation:', node.total_minted)
            elif cmd[0] == 'peers':
                print('Peers:', node.peers)
            elif cmd[0] == 'exit':
                break
            else:
                print('Unknown command')
        except Exception as e:
            print('Error:', e)

if __name__ == '__main__':
    main()

