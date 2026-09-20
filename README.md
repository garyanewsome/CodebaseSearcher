# CodebaseSearcher

A tool Hermes can call — not a standalone app. You ask Hermes to look at a
repo; it clones it, semantically searches it for whatever you asked about,
and writes the findings into your Obsidian vault as a note. Not a
continuously-synced index like Athenaeum's vault search — this is
on-demand, one repo at a time, when you actually want it looked at.

## What it does

- `POST /research {"repo": "...", "question": "..."}` — clones/pulls the repo (GitHub, same account the homelab already has SSH access to), re-indexes it only if the commit actually changed since last time, semantically searches for `question`, and writes a findings note into the vault. Returns the matched snippets plus the note's path.
- `POST /forget {"repo": "..."}` — deletes the repo's local clone and its embedding index. Does **not** touch any findings note already written to the vault — those are yours to keep or delete like any other note; this only cleans up the disposable, regenerable cache.
- `GET /health`

## Why on-demand, not a background sync

Athenaeum's vault sync runs hourly because the vault changes constantly and staying current matters. Repos you ask Hermes to "check out" are the opposite — you look at one, get what you need, move on. Keeping every repo you've ever mentioned indexed and fresh in the background would mean constant re-embedding for repos nobody's asking about. Indexing happens exactly when `/research` is called, gated on whether the repo's commit hash actually moved since the last time.

## Storage

Chroma, embedded in-process — same pattern as Athenaeum, not a new database service to stand up and operate. One collection per repo (`repo-<name>`), so re-indexing one repo never touches another's data. Everything (repo clones, the Chroma index, the vault's own working clone) lives under `DATA_DIR`, which on the homelab is a PVC backed by `/mnt/storage`, not root — see Hermes's own README for why that distinction actually matters there (root ran to 94% full before it got resized).

## Embeddings

`jinaai/jina-embeddings-v2-base-code` via `sentence-transformers` (`trust_remote_code=True` — it's a Jina-custom architecture, not vanilla BERT), CPU only. This never runs on the interactive chat path — it's a batch job triggered by an explicit "check out this repo" ask — so there's no reason to contend with Ollama/Iris for the homelab's one shared GPU, and no VRAM handoff dance to replicate. Verified locally: code-to-matching-description similarity ~0.71, code-to-unrelated ~0.17 — real semantic separation, not just keyword overlap.

Swap the model via `EMBEDDING_MODEL` if needed; `app/embeddings.py` is a thin, swappable wrapper, same shape as `skunkworks_ai_rag`'s.

## Chunking

Line-based with blank-line-aware boundaries (`app/chunking.py`) — not AST/tree-sitter parsing. Same pragmatic bar as Athenaeum's own markdown chunker, which its own README is upfront about being "functional, not tuned." Good enough to find the right file and the right neighborhood of it. Revisit with real per-language parsing if retrieval quality turns out to actually suffer in practice — not before.

## Writing to the vault

A **separate** git clone and a **separate**, write-scoped-to-only-this-repo GitHub deploy key from Athenaeum's — Athenaeum's own key is deliberately read-only, and this needed write access, so rather than upgrading a key that has no other reason to ever write anything, this gets its own. Least-privilege: if this service is ever compromised or buggy, the blast radius is "can push new files to one repo," not "can read or write anything the account key can touch."

Every write is a **brand-new, uniquely-named file** — `<repo>-<topic>-<date>.md`, incrementing a suffix if that exact name's already taken (e.g. a second research pass on the same repo/topic same day) — never an edit to an existing note. That's the whole reason a push conflict is rare: git only has something to reconcile when the *same* file changed on both sides, and a fresh file never collides with whatever you're editing live in Obsidian on your own devices. On a rejected push (something else landed on the remote in between pull and push), it does one `pull --rebase` and retries once before giving up.

Verified end to end against a throwaway fake vault (a local bare git repo, never the real one): a finding actually landed on the "remote," confirmed via an independent fresh clone — not just a local working-directory illusion — and a second write for the same repo/topic/day got a distinct filename rather than silently overwriting the first.

## Running it locally

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=./data GITHUB_SSH_KEY_PATH=~/.ssh/id_ed25519 .venv/bin/uvicorn app.main:app --reload --port 8002
```

Point `GITHUB_SSH_KEY_PATH` at whatever key your own machine already uses for GitHub — locally there's no need for the homelab's dedicated key.

## Deployment

Runs in K3s, internal-only — no NodePort or Ingress, since nothing outside the cluster ever talks to this directly, only Hermes does.

- `k8s/codebase-searcher-api.yaml` — Deployment + Service + PersistentVolume/Claim (backed by `/mnt/storage/codebase-searcher-data`)

**One-time setup before the first deploy** (needs `kubectl`, homelab's own terminal only):

```bash
# Account-level key already on the homelab host — reused as-is, read-only
# in the sense that it only ever clones/pulls research targets, never
# pushes anywhere.
kubectl create secret generic github-ssh-key \
  --from-file=id_ed25519_github=$HOME/.ssh/id_ed25519_github

# Dedicated write-scoped deploy key for the vault — generated specifically
# for this service; the public half needs adding as a deploy key on the
# obsidian-vault GitHub repo with "Allow write access" checked.
kubectl create secret generic vault-write-key \
  --from-file=vault_write=$HOME/.ssh/codebase-searcher/vault_write

kubectl apply -f k8s/codebase-searcher-api.yaml
```

To redeploy after a code change: `./deploy.sh`.

## Hermes integration

Two tools in Hermes's `app/tools.py`: `research_repo(repo, question)` and `forget_repo(repo)`, calling this service's `/research` and `/forget`. `CODEBASE_SEARCHER_URL` in Hermes's `app/config.py` points at it over in-cluster service DNS.

## Status

- [x] Chunking, embedding, indexing, semantic search — verified against a real test repo, including that a discount-pricing question correctly ranked the pricing function far above an unrelated greeting function
- [x] Vault write-back — verified end to end against a throwaway fake vault, including the push-retry path and same-day dedup naming
- [ ] Not yet deployed to K3s — needs the two deploy-key secrets created first (see Deployment)
- [ ] Not yet wired into Hermes's actual running deployment (tool functions added; needs Hermes redeployed with `CODEBASE_SEARCHER_URL` pointed at this service)
