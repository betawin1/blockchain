import hashlib
import json
import os
import threading
import time
import argparse
from collections import defaultdict
from uuid import uuid4
from flask import Flask, request, jsonify
import requests

# ----- CONFIGURATION -----
MAX_SUPPLY = 21_000_000
BLOCK_REWARD = 6.25
DIFFICULTY = 4         # number of leading zeros
STORAGE_FILE = 'chain_state.json'
BOOTSTRAP_URL = None   # URL to JSON list of bootstrap nodes
TRACKER_URL = None     # Base URL of tracker API

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

    def to_dict(self):
        return vars(self)

class Blockchain:
    def __init__(self):
        self.chain = []
        self.pending_tx = []
        self.balances = defaultdict(float)
        self.total_minted = 0.0
        self.peers = set()
        self.load_state()

    def create_genesis(self):
        genesis = Block(0, '0'*64, [])
        self.chain = [genesis]
        self.save_state()

    def add_transaction(self, sender, recipient, amount):
        if self.balances[sender] < amount:
            raise ValueError('Insufficient balance')
        tx = {'sender': sender, 'recipient': recipient, 'amount': amount}
        self.pending_tx.append(tx)
        self.save_state()
        return tx

    def mine_pending(self, miner_address):
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
        return block

    def is_valid_block(self, blk):
        if blk['index'] != len(self.chain):
            return False
        prev = self.chain[-1]
        if blk['prev_hash'] != prev.hash:
            return False
        reconstructed = Block(blk['index'], blk['prev_hash'], blk['transactions'],
                              timestamp=blk['timestamp'], nonce=blk['nonce'])
        return reconstructed.hash == blk['hash']

    def register_peer(self, host):
        self.peers.add(host)
        self.save_state()

    def broadcast(self, path, data):
        for host in list(self.peers):
            try:
                url = f"{host}{path}"
                requests.post(url, json=data, timeout=3)
            except:
                continue

    # ----- Bootstrap & Tracker -----
    def load_bootstrap(self, url):
        try:
            nodes = requests.get(url, timeout=5).json()
            for n in nodes:
                self.peers.add(n['host'])
        except Exception as e:
            print('Bootstrap load failed:', e)

    def register_tracker(self, url, public_url):
        try:
            requests.post(f"{url}/register", json={'host': public_url}, timeout=5)
        except Exception as e:
            print('Tracker register failed:', e)

    def fetch_tracker(self, url):
        try:
            peers = requests.get(f"{url}/peers", timeout=5).json()
            for p in peers:
                self.peers.add(p['host'])
        except Exception as e:
            print('Tracker fetch failed:', e)

    # ----- Persistence -----
    def save_state(self):
        state = {
            'chain': [b.to_dict() for b in self.chain],
            'pending_tx': self.pending_tx,
            'balances': dict(self.balances),
            'total_minted': self.total_minted,
            'peers': list(self.peers)
        }
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
            self.peers = set(data.get('peers', []))

# ----- Flask API -----
app = Flask(__name__)
node = Blockchain()

@app.route('/peers', methods=['GET'])
def get_peers():
    return jsonify(list(node.peers))

@app.route('/tx', methods=['POST'])
def new_tx():
    data = request.get_json()
    tx = node.add_transaction(data['sender'], data['recipient'], data['amount'])
    node.broadcast('/tx', tx)
    return jsonify(tx), 201

@app.route('/mine', methods=['POST'])
def mine():
    data = request.get_json()
    miner = data.get('miner')
    blk = node.mine_pending(miner)
    blk_dict = blk.to_dict()
    node.broadcast('/block', blk_dict)
    return jsonify(blk_dict), 201

@app.route('/block', methods=['POST'])
def new_block():
    blk = request.get_json()
    if node.is_valid_block(blk):
        node.chain.append(Block(blk['index'], blk['prev_hash'], blk['transactions'], blk['timestamp'], blk['nonce']))
        for tx in blk['transactions']:
            node.balances[tx['sender']] -= tx['amount']
            node.balances[tx['recipient']] += tx['amount']
        node.total_minted += min(BLOCK_REWARD, MAX_SUPPLY - node.total_minted)
        node.pending_tx = []
        node.save_state()
    return '', 204

@app.route('/chain', methods=['GET'])
def get_chain():
    return jsonify([b.to_dict() for b in node.chain])

@app.route('/state', methods=['GET'])
def get_state():
    return jsonify({
        'balances': dict(node.balances),
        'pending_tx': node.pending_tx,
        'total_minted': node.total_minted
    })

# ----- MAIN -----
def main():
    parser = argparse.ArgumentParser(description='HTTP Blockchain Node')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--public', help='Public URL of this node, e.g. https://myapp.onrender.com')
    parser.add_argument('--bootstrap', help='URL to bootstrap JSON list')
    parser.add_argument('--tracker', help='Base URL of tracker API')
    parser.add_argument('--wallet', help='Your wallet address')
    args = parser.parse_args()

    global BOOTSTRAP_URL, TRACKER_URL
    BOOTSTRAP_URL = args.bootstrap
    TRACKER_URL = args.tracker

    wallet = args.wallet or str(uuid4())
    print('Wallet:', wallet)

    if BOOTSTRAP_URL:
        node.load_bootstrap(BOOTSTRAP_URL)
    if TRACKER_URL and args.public:
        node.register_tracker(TRACKER_URL, args.public)
        node.fetch_tracker(TRACKER_URL)

    # register self as peer
    if args.public:
        node.register_peer(args.public)

    app.run(host=args.host, port=args.port)

if __name__ == '__main__':
    main()
