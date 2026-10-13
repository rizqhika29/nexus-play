# NexusPlay

An *Interoperable Games with NFTs* contract on GenLayer. Games register once and **auto-generate** NFT items for players, the network **recognizes** each issued item against the game's published specification, and players **move their assets between games** inside the same ecosystem.

## Deployed Contract

**Address:** `0x5735615272496F6f7E4ca2e9037f6d84356b881E`
**Explorer:** https://explorer-studio.genlayer.com/address/0x5735615272496F6f7E4ca2e9037f6d84356b881E

## How It Works

1. A publisher calls `register_game(game_id, name, catalog_url, item_classes)` - only the subset of the ecosystem taxonomy (`weapon`, `armor`, `mount`, `cosmetic`, `material`, `relic`) that the game may issue is declared up front.
2. `mint_item(game_id, recipient, seed, spec_url)` derives every attribute from `keccak(game_id:seed:index)`: class (from the game's declared classes), rarity (60/20/14/5/1 percent buckets) and power (1-100). No AI, fully reproducible - `tests/direct/test_helpers.py` proves anybody can re-derive `item-7` from public inputs alone.
3. `recognize_item(token_id)` runs a non-deterministic block: each validator fetches the item's spec URL *and* the game's catalog URL, and a model reports what the game actually published. Consensus is stored only when both validators agree **exactly** on `matched`, class, rarity, power and on the contract-measured count of sources that actually fetched (`MIN_REACHABLE_SOURCES = 2`), with the result pinned to the exact URLs it was read from. The write path then re-checks it *deterministically* against the minted attributes and stores the **minted** values (the published power is kept in `reported_power` for audit, within `POWER_TOLERANCE = 5`) - consensus can confirm an item, never rewrite it, and `get_recognition()` can never contradict `get_item()`.
4. `transfer_item` moves ownership player-to-player; `bridge_item(token_id, target_game_id)` relocates a recognized item into another active game that accepts its class, keeping token id and owner. `can_bridge` is a view over the exact same `_bridge_blocker` helper the transaction uses, so the view can never promise a bridge the write path refuses. A publisher may repair a dead item page with `update_item_spec_url` *before* recognition; the evidence URL is frozen afterwards.

## Security & Consensus Hardening

Reviewed against the GenLayer staff review checklist before the first deployment:

- **No invented readings.** The prompt forbids guessing a power: an unstated power must be reported as `0`, which the shape check rejects, so a silent default can never become a "confirmed" attribute.
- **Reachability is measured, not claimed.** The contract counts the fetches that actually succeeded (`reachable`) and pins the two URLs actually read (`sources`); neither field comes from the model, and `_consensus_ok` re-verifies both.
- **Exact consensus, lenient backstop.** Leader and validator must agree on the published power *exactly* (no tolerance inside consensus). `POWER_TOLERANCE` only applies between the published reading and the minted value, and the stored recognition is always the minted value.
- **Supported clock only.** `minted_at` / `recognized_at` are parsed from the transaction datetime GenVM pins into `gl.message_raw["datetime"]` with pure integer math - no `datetime.now()`, no wall clock that could make validators disagree.
- **https-only evidence.** Spec URLs must be https: recognition evidence is re-fetched by validators, and a plaintext page could be rewritten between mint and audit.
- **No evidence replacement.** `update_item_spec_url` is restricted to the origin-game publisher and to items still `issued`; once recognized, the URL is immutable.
- **No value, no escrow.** The contract never holds or moves funds, so there is no deposit to lock and no timeout path to add. Items stay transferable even while their game is paused, and `recognize_item` is idempotent (a second call reverts).
- **Caller-bound writes.** Minting is publisher-only, status changes are publisher-only, transfers and bridges are owner-only, seed reuse reverts, zero-address recipients revert, and every write validates its arguments before touching storage.
- **Bounded views.** `get_owner_items_page` clamps `offset`/`limit` so a hostile caller can never force an unbounded scan.

## Tests

**Direct tests (123):**
```bash
python -m pytest tests/direct/ -q
```

Helper unit tests (`tests/direct/test_helpers.py`) run with a stubbed SDK; lifecycle tests (`tests/direct/test_nexus_play.py`) cover registry, status, minting, consensus recognition (including validator divergence and exact-agreement checks), spec-URL repair, transfer, bridging, views and the bridge gates.

**Integration tests (14) against the deployment:**
```bash
NEXUS_PLAY_ADDRESS=0x5735615272496F6f7E4ca2e9037f6d84356b881E python -m pytest tests/integration/
```

**Live tests (47 checks) driving every method on-chain, including the real AI consensus recognition and the bridge it unlocks:**
```bash
$env:NEXUS_PLAY_ADDRESS="0x5735615272496F6f7E4ca2e9037f6d84356b881E"
python tools/live_test.py
```

The live test publishes the recognition evidence through `https://httpbin.org/base64/<json>` (the publisher derives the item attributes locally and serves them there), so validators re-fetch exactly what the game published - no private hosting required.

**Lint:**
```bash
genvm-lint contracts/nexus_play.py
```

## Deployment

```bash
set PRIVATE_KEY_1=...
set PRIVATE_KEY_2=...
set TESTNET_PRIVATE_KEY=...
python tools/deploy.py
```

The script prints `NEXUS_PLAY_ADDRESS=0x...`; feed that to the integration run above. `NEXUS_PLAY_ECOSYSTEM` overrides the constructor's ecosystem name (default `NexusPlay Ecosystem`).

## Layout

```
contracts/nexus_play.py        # contract
tests/direct/                  # fast VM tests + pure helper tests
tests/integration/             # Studio tests (gated on NEXUS_PLAY_ADDRESS)
tools/deploy.py                # Studio deployment with RPC throttling
tools/live_test.py             # every-method on-chain test (incl. AI consensus)
deployment.toml                # runtime / consensus configuration
gltest.config.yaml             # networks + contract path
```

## Limitations

Recognition verifies that a game *published* a coherent spec, not that the underlying art or metadata is authentic - a publisher controls its own URLs. Spec URLs must stay reachable (`https`), since both sources are re-fetched on every recognition. Identical `(game_id, seed)` pairs can only be minted once, so the seed is the publisher's public entropy for that item. Item specs are embedded into the model prompt unsanitized, so a hostile page can attempt prompt injection; composing games should point `spec_url` at machine-readable, reviewed endpoints (JSON over HTTPS).
