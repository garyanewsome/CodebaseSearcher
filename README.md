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

Chroma, embedded in-process — same pattern as Athenaeum, not a new database service to stand up and operate. One collection per repo (`repo-<name>`), so re-indexing one repo never touches another's data. Everything (repo clones, the Chroma index, the vault's own working clone) lives under `DATA_DIR`, which on the homelab is `/mnt/storage/codebase-searcher-data` directly — a plain host path now that this runs bare-metal, not a K8s PVC. Still deliberately not root, which Hermes's own README documents running dangerously low on.

## Embeddings

`jinaai/jina-embeddings-v2-base-code` via `sentence-transformers` (`trust_remote_code=True` — it's a Jina-custom architecture, not vanilla BERT), `batch_size=8`, GPU-accelerated (`device` auto-detects `cuda`, falling back to `cpu` for local dev on a Mac). Verified locally: code-to-matching-description similarity ~0.71, code-to-unrelated ~0.17 — real semantic separation, not just keyword overlap.

**GPU handoff.** A real CPU-only run took 552.7s to embed a mid-size repo (see Status) — too slow for a synchronous tool call, and it pegged the same CPU Ollama's chat model runs on. This now runs bare-metal on the homelab host and shares the RTX 3060 with Ollama/Iris, using the identical handoff pattern already proven for `generate_image`: Hermes unloads Ollama's model (`keep_alive: 0`) right before calling `/research`, and calls this service's own `POST /unload` right after — which drops the cached `SentenceTransformer` and calls `torch.cuda.empty_cache()` — so the GPU is free again before Ollama reloads for the follow-up reply. See `app/embeddings.py` (`unload_model`) and Hermes's `app/chat.py` (`_unload_codebase_searcher`).

Swap the model via `EMBEDDING_MODEL` if needed; `app/embeddings.py` is a thin, swappable wrapper, same shape as `skunkworks_ai_rag`'s.

## Chunking

Line-based with blank-line-aware boundaries (`app/chunking.py`) — not AST/tree-sitter parsing. Same pragmatic bar as Athenaeum's own markdown chunker, which its own README is upfront about being "functional, not tuned." Good enough to find the right file and the right neighborhood of it. Revisit with real per-language parsing if retrieval quality turns out to actually suffer in practice — not before.

A hard 6500-char ceiling (`CHUNK_HARD_MAX_CHARS`) caps every chunk regardless of blank-line availability. Found the hard way: a real repo (dragonfly-reverb) had a dense C++ header with no blank-line break for ~9000 chars, and `sentence_transformers.encode()` pads every sequence in a batch to that batch's longest member before the forward pass — so one such outlier blows up attention memory for its *entire* batch (`batch_size × max_len²`), not just itself. That OOM-killed the process on the homelab's 14GB box. The hard cap plus the reduced batch size above fixed it.

## Writing to the vault

A **separate** git clone and a **separate**, write-scoped-to-only-this-repo GitHub deploy key from Athenaeum's — Athenaeum's own key is deliberately read-only, and this needed write access, so rather than upgrading a key that has no other reason to ever write anything, this gets its own. Least-privilege: if this service is ever compromised or buggy, the blast radius is "can push new files to one repo," not "can read or write anything the account key can touch."

Every write is a **brand-new, uniquely-named file** — `<repo>-<topic>-<date>.md`, incrementing a suffix if that exact name's already taken (e.g. a second research pass on the same repo/topic same day) — never an edit to an existing note. That's the whole reason a push conflict is rare: git only has something to reconcile when the *same* file changed on both sides, and a fresh file never collides with whatever you're editing live in Obsidian on your own devices. On a rejected push (something else landed on the remote in between pull and push), it does one `pull --rebase` and retries once before giving up.

Verified end to end against a throwaway fake vault (a local bare git repo, never the real one): a finding actually landed on the "remote," confirmed via an independent fresh clone — not just a local working-directory illusion — and a second write for the same repo/topic/day got a distinct filename rather than silently overwriting the first.

## Running it locally

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=./data GITHUB_SSH_KEY_PATH=~/.ssh/id_ed25519 .venv/bin/uvicorn app.main:app --reload --port 8002
```

Point `GITHUB_SSH_KEY_PATH` at whatever key your own machine already uses for GitHub — locally there's no need for the homelab's dedicated key. On a Mac (no CUDA) this falls back to CPU automatically — fine for quick local checks, just slow on anything but a handful of chunks.

## Deployment

Bare-metal on the homelab host, not K3s — same reason as Iris: embedding needs the GPU, and the cluster has no device plugin configured for it. Reachable on the LAN by IP:port (`http://192.168.1.157:8300`), no Ingress/hostname.

```bash
sudo systemctl status codebase-searcher
sudo systemctl restart codebase-searcher
```

To redeploy after a code change: `./deploy.sh` — pulls, reinstalls dependencies, restarts the service.

**One-time setup** (homelab's own terminal):

```bash
git clone git@github.com:garyanewsome/CodebaseSearcher.git ~/CodebaseSearcher
cd ~/CodebaseSearcher

python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

mkdir -p /mnt/storage/codebase-searcher-data
```

The two GitHub keys just need to already exist at the paths `app/config.py` expects (see there) — the account-level key (`~/.ssh/id_ed25519_github`) almost certainly already does; the vault write-scoped key needs generating once if it doesn't exist yet:

```bash
mkdir -p ~/.ssh/codebase-searcher
ssh-keygen -t ed25519 -f ~/.ssh/codebase-searcher/vault_write -N ""
# add the .pub half as a deploy key on the obsidian-vault repo, with
# "Allow write access" checked
```

Then create the systemd unit (`/etc/systemd/system/codebase-searcher.service`), matching Iris's shape:

```ini
[Unit]
Description=CodebaseSearcher service
After=network-online.target

[Service]
User=garyanewsome
WorkingDirectory=/home/garyanewsome/CodebaseSearcher
ExecStart=/home/garyanewsome/CodebaseSearcher/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8300
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codebase-searcher
```

If this was previously deployed to K3s (an earlier iteration was), the two K8s secrets (`github-ssh-key`, `vault-write-key`) and anything from `kubectl apply` are no longer used and can be deleted with `kubectl delete secret github-ssh-key vault-write-key` — harmless to leave them too, just dead weight.

## Hermes integration

Two tools in Hermes's `app/tools.py`: `research_repo(repo, question)` and `forget_repo(repo)`, calling this service's `/research` and `/forget`. `CODEBASE_SEARCHER_URL` in Hermes's `app/config.py` points at it by LAN IP:port (`http://192.168.1.157:8300`), same as `IRIS_URL`. `research_repo` gets the same GPU handoff as `generate_image` in Hermes's `app/chat.py` — see the GPU handoff note above.

## Status

- [x] Chunking, embedding, indexing, semantic search — verified against a real test repo, including that a discount-pricing question correctly ranked the pricing function far above an unrelated greeting function
- [x] Vault write-back — verified end to end against a throwaway fake vault, including the push-retry path and same-day dedup naming
- [x] Chunk-size OOM fix verified on the real homelab hardware: encoding dragonfly-reverb's 284 chunks held around 1.7GB RSS on CPU — no OOM kill (previously crashed at 12.3GB)
- [x] Moved to bare-metal/GPU after that same CPU run measured 552.7s (~9 min) for 284 chunks — too slow for a synchronous tool call. GPU handoff code written (`unload_model`/`/unload`, mirroring Iris); **not yet re-benchmarked on the actual RTX 3060** — do that once deployed, before considering this fully verified
- [ ] Not yet deployed — needs the one-time systemd setup above run on the homelab
- [ ] Not yet wired into Hermes's actual running deployment (tool + handoff code written and pushed; needs Hermes redeployed to pick it up)
