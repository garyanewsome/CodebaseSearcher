import os

# Where cloned repos, their Chroma indexes, and the vault clone all live —
# deliberately NOT the container's own ephemeral filesystem. On the
# homelab this is backed by /mnt/storage, not root (which Hermes's own
# README documents running dangerously low on).
DATA_DIR = os.environ.get("DATA_DIR", "./data")

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "jinaai/jina-embeddings-v2-base-code")

GITHUB_USER = os.environ.get("GITHUB_USER", "garyanewsome")
# The account-level key — already has read access to every repo on the
# account, so any repo can be researched on demand without adding a new
# per-repo deploy key each time one comes up.
GITHUB_SSH_KEY_PATH = os.environ.get("GITHUB_SSH_KEY_PATH", "/secrets/github/id_ed25519_github")

VAULT_REPO_URL = os.environ.get("VAULT_REPO_URL", "git@github.com:garyanewsome/obsidian-vault.git")
# Deliberately a separate, write-scoped-to-only-this-repo deploy key, not
# the account-level key above — the account key can read anything, which
# is exactly why it shouldn't also be the one with write access to
# something as personal as the vault.
VAULT_SSH_KEY_PATH = os.environ.get("VAULT_SSH_KEY_PATH", "/secrets/vault-write/vault_write")
VAULT_FINDINGS_FOLDER = os.environ.get("VAULT_FINDINGS_FOLDER", "10 Development/Codebase Findings")
