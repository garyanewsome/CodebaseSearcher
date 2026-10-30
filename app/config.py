import os

# Where cloned repos and their Chroma indexes live.
# Bare-metal on the homelab host (not K3s — embedding needs the GPU, and
# the cluster has no device plugin for it), so this is a plain host path:
# /mnt/storage directly, not root (which Hermes's own README documents
# running dangerously low on).
DATA_DIR = os.environ.get("DATA_DIR", "/mnt/storage/codebase-searcher-data")

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "jinaai/jina-embeddings-v2-base-code")

GITHUB_USER = os.environ.get("GITHUB_USER", "garyanewsome")
# The account-level key — already has read access to every repo on the
# account, so any repo can be researched on demand without adding a new
# per-repo deploy key each time one comes up. Bare-metal means this is
# just the key already sitting in the host's own home directory, no
# secret-mount indirection needed.
GITHUB_SSH_KEY_PATH = os.environ.get("GITHUB_SSH_KEY_PATH", "/home/garyanewsome/.ssh/id_ed25519_github")

# The vault itself (git clone, write-scoped deploy key, write limits, audit
# log) is vault-keeper's; this service only asks it to write findings notes.
VAULT_KEEPER_URL = os.environ.get("VAULT_KEEPER_URL", "http://192.168.1.157:8310")
VAULT_FINDINGS_FOLDER = os.environ.get("VAULT_FINDINGS_FOLDER", "10 Development/Codebase Findings")
