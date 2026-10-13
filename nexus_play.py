# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
NexusPlay
=========

An *Interoperable Games with NFTs* primitive for the GenLayer ecosystem:
games register, **auto-generate** NFT items for players, the network
**recognizes** each issued item against the game's published specification,
and players **move their assets seamlessly between games** inside the same
ecosystem.

What the contract actually does
-------------------------------
1. **Game registry.** A publisher registers a game once (`game_id`, display
   name, published catalog URL, and the subset of the ecosystem item taxonomy
   the game is allowed to issue). The publisher is the only address that can
   mint for that game or pause it.
2. **Auto-generated items.** `mint_item` derives every attribute from a
   keccak hash of ``game_id:seed:index``: item class (drawn from the classes
   the game declared at registration), rarity (weighted buckets: 60/20/14/5/1
   percent) and power (1-100). No AI is involved, so minting is deterministic,
   reproducible and unit-testable -- anybody can re-derive the attributes of
   `item-7` from the public game id, seed and index.
3. **Consensus recognition.** `recognize_item` runs a non-deterministic block:
   every validator independently fetches the item's published spec URL *and*
   the game's catalog URL, and a model reports what the game actually
   published (canonical class, rarity, power, whether the page confirms this
   token). The leader's answer is stored only when both validators agree on
   `matched`, class, rarity and power EXACTLY, and on the deterministic count
   of sources that actually fetched (the contract counts the fetches itself --
   the model never reports reachability). The result is additionally bound to
   the exact URLs it was read from. The contract then re-checks the answer
   *deterministically* against the attributes it derived at mint time, and
   stores the minted values (keeping the published power in `reported_power`)
   -- a consensus result that disagrees with the minted item can never be
   stored, and `get_recognition()` can never contradict `get_item()`.
 4. **Seamless movement.** `transfer_item` moves ownership player-to-player.
    `bridge_item` relocates a recognized item into another active game that
    accepts its class, keeping the same token id and owner: the player's asset
    crosses game boundaries without a wrap/unwrap step. A publisher may repair
    a dead item page with `update_item_spec_url` *before* recognition; the
    evidence URL is frozen afterwards.

 Why this is more than "AI decides X"
 ------------------------------------
 - **Taxonomy is code, not prompt.** `ITEM_CLASSES` / `RARITIES` are module
   constants. The model must answer *inside* that vocabulary; anything else
   reverts before storage.
 - **The model never sees the expected answer.** The prompt carries the token
   id and the raw published pages, not the minted attributes. It has to read
   the spec the game published, which is exactly what "recognition" means.
 - **No invented readings.** The prompt forbids guessing a power value: an
   unstated power must be reported as 0, which the shape check rejects, so a
   silent default can never become a "confirmed" attribute.
 - **Deterministic backstop.** After consensus, the write path compares the
   agreed class/rarity/power with the values the contract derived at mint.
   Consensus can therefore *confirm* an item, never *rewrite* it.
 - **Two-source liveness.** Both the item page and the game catalog must be
   reachable (`MIN_REACHABLE_SOURCES`); the count is measured by the contract
   and is part of consensus, so one validator can't quietly work from a
   degraded view.
 - **Pure helpers.** `_derive_attributes`, `_rarity_for_roll`,
   `_bridge_blocker`, `_consensus_ok`, `_validate_*` are I/O-free and tested in
   `tests/direct/test_helpers.py` without a VM.

Trust model / limitations
-------------------------
- Recognition verifies that the game *published* a coherent spec, not that
  the underlying art/metadata is authentic. A publisher controls its own
  spec URLs; the ecosystem's defence is that its own published pages are
  re-read by independent validators and are publicly auditable.
- Item specs are embedded into the model prompt unsanitized, so a hostile
  page can attempt prompt injection. Composing games should point `spec_url`
  at machine-readable, reviewed endpoints (JSON over HTTPS).
 - The rarity/power distribution is a property of keccak output, not of any
   game's balance design; a game that wants different odds must mint with
   seeds it is willing to be held to (attributes are reproducible from public
   inputs).
 - The contract never holds or moves value, so there is no escrow to unlock
   and no timeout path to add: the only assets are the item records
   themselves, and they stay transferable even while their game is paused.
 - `recognize_item` is intentionally permissionless (anyone may pay for the
   consensus run). It is idempotent -- a second call reverts -- and the stored
   result is content-bound to the token's own spec, so triggering it cannot
   change what any other item means.
 - Spec URLs must be https: recognition evidence is re-fetched by validators,
   and a plaintext page could be rewritten between the mint and the audit.
"""

from genlayer import *
from dataclasses import dataclass
import json

# --- Tunable constants -------------------------------------------------------

# Ecosystem-wide item taxonomy. Recognition results outside it are rejected.
ITEM_CLASSES = ("weapon", "armor", "mount", "cosmetic", "material", "relic")

# Rarity buckets, most common first. RARITY_BOUNDS are cumulative basis
# points (out of 10000): 60% common, 20% uncommon, 14% rare, 5% epic, 1%
# legendary.
RARITIES = ("common", "uncommon", "rare", "epic", "legendary")
RARITY_BOUNDS = (6000, 8000, 9400, 9900, 10000)

GAME_STATUSES = ("active", "paused")
ITEM_STATUSES = ("issued", "recognized")

MAX_ECOSYSTEM_NAME = 48
MAX_GAME_ID = 40
MAX_TITLE = 64
MAX_SEED = 64
MAX_CLASSES_PER_GAME = 6
MAX_URL_CHARS = 200

# Page excerpt fed to the model (bounds the prompt size).
MAX_SPEC_CHARS = 4000

# Published power may deviate from the minted power by at most this much
# before recognition is refused. This is publisher *leniency* only: consensus
# itself must agree on the exact published reading (see
# `_consensus_validator`), and the value stored for the item is always the
# minted power -- the reading is kept in `reported_power` for audit.
POWER_TOLERANCE = 5

# Both the item page and the game catalog have to be reachable.
MIN_REACHABLE_SOURCES = 2

# Page size ceiling for the paginated owner-items view.
MAX_PAGE = 50


# --- Deterministic helpers ----------------------------------------------------


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's
    inverse-JDN algorithm). Pure integer math: no stdlib calendar, no wall
    clock, so every validator computes the same number."""
    y = year - (1 if month <= 2 else 0)
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _current_timestamp() -> u256:
    """Supported on-chain time source: the transaction datetime GenVM pins
    into ``gl.message_raw["datetime"]`` (ISO-8601, e.g.
    ``2026-10-13T09:30:00Z``). Every validator executing the same transaction
    is handed the identical string, so the parsed value is consensus-safe.

    Pure string/integer parsing -- no ``datetime.now()``, no module import,
    and no wall-clock reads that would make validators disagree."""
    raw = str(gl.message_raw["datetime"])
    # Expected shape: "YYYY-MM-DDTHH:MM:SS[.ffffff][Z|+HH:MM]"
    if len(raw) < 19 or raw[4] != "-" or raw[7] != "-":
        raise gl.vm.UserError("transaction datetime is malformed")
    year = int(raw[0:4])
    month = int(raw[5:7])
    day = int(raw[8:10])
    hour = int(raw[11:13])
    minute = int(raw[14:16])
    second = int(raw[17:19])
    if not (
        1 <= month <= 12
        and 1 <= day <= 31
        and 0 <= hour <= 23
        and 0 <= minute <= 59
        and 0 <= second <= 60
    ):
        raise gl.vm.UserError("transaction datetime is malformed")
    return u256(
        _days_from_civil(year, month, day) * 86400
        + hour * 3600
        + minute * 60
        + second
    )


def _coerce_address(value) -> Address:
    """Normalize an address argument (Address, hex/base64 str, raw int or
    buffer) so write methods don't break depending on how the caller encoded
    it."""
    if isinstance(value, Address):
        return value
    if isinstance(value, str):
        return Address(value)
    if isinstance(value, int):
        return Address(value.to_bytes(20, "big"))
    return Address(bytes(value))


def _is_zero_address(addr: Address) -> bool:
    return addr.as_bytes == b"\x00" * 20


def _validate_text(value, field: str, max_len: int) -> str:
    """Deterministic text validation shared by registration and minting.
    No I/O. Rejects empty / over-long values."""
    text = str(value).strip()
    if not text:
        raise gl.vm.UserError(f"{field} must not be empty")
    if len(text) > max_len:
        raise gl.vm.UserError(f"{field} too long (max {max_len} chars)")
    return text


def _validate_game_id(game_id) -> str:
    """Game ids are the prefix of every derived item key, so they may not
    contain ':' -- otherwise ``game "a:b" seed "c"`` and ``game "a" seed
    "b:c"`` would hash the same material."""
    game_id = _validate_text(game_id, "game_id", MAX_GAME_ID)
    if ":" in game_id:
        raise gl.vm.UserError("game_id must not contain ':'")
    return game_id


def _validate_seed(seed) -> str:
    seed = _validate_text(seed, "seed", MAX_SEED)
    return seed


def _validate_url(url) -> str:
    """Deterministic https-only URL validation. No I/O.

    ``spec_url`` is the evidence recognition re-reads, so a plaintext hop is
    refused: an http page can be rewritten in transit and would let a network
    attacker feed validators a different "published" spec."""
    url = str(url).strip()
    if not url.startswith("https://") or url == "https://":
        raise gl.vm.UserError(f"invalid spec URL: {url!r}")
    if len(url) > MAX_URL_CHARS:
        raise gl.vm.UserError(f"spec URL too long (max {MAX_URL_CHARS} chars)")
    return url


def _validate_classes(classes) -> list[str]:
    """Validate a game's declared item classes: non-empty, de-duplicated,
    bounded, and drawn from the ecosystem taxonomy."""
    if not classes:
        raise gl.vm.UserError("a game must declare at least one item class")
    if len(classes) > MAX_CLASSES_PER_GAME:
        raise gl.vm.UserError(f"at most {MAX_CLASSES_PER_GAME} item classes allowed")
    validated: list[str] = []
    for cls in classes:
        cls = str(cls).strip().lower()
        if cls not in ITEM_CLASSES:
            raise gl.vm.UserError(f"unknown item class: {cls!r}")
        if cls not in validated:
            validated.append(cls)
    if len(validated) != len(classes):
        raise gl.vm.UserError("item classes must be unique")
    return validated


def _seed_key(game_id: str, seed: str) -> str:
    """Storage key that makes (game, seed) unique -- one seed, one item."""
    return f"{game_id}:{seed}"


def _rarity_for_roll(roll: int) -> str:
    """Map a 0-9999 roll onto the rarity buckets."""
    if roll < 0:
        roll = 0
    for rarity, bound in zip(RARITIES, RARITY_BOUNDS):
        if roll < bound:
            return rarity
    return RARITIES[-1]


def _derive_attributes(item_key: str, classes: list[str]) -> tuple[str, str, int]:
    """Auto-generate an item's attributes from its public derivation key.

    ``Keccak256(item_key)`` is split into independent fields:
      bytes 0-1  -> rarity roll   (mod 10000, bucketed by RARITY_BOUNDS)
      bytes 2-3  -> power         (mod 100, +1 => 1..100)
      bytes 4-5  -> class index   (mod len(classes))

    Pure function: no storage, no I/O, no AI. Raises ValueError for an empty
    class list (unreachable on-chain: a game must declare classes)."""
    if not classes:
        raise ValueError("classes must not be empty")
    digest = Keccak256(item_key.encode("utf-8")).digest()
    roll = int.from_bytes(digest[0:2], "big") % 10000
    power = (int.from_bytes(digest[2:4], "big") % 100) + 1
    item_class = classes[int.from_bytes(digest[4:6], "big") % len(classes)]
    return item_class, _rarity_for_roll(roll), power


def _bridge_blocker(
    item_status: str,
    home_game: str,
    item_class: str,
    target_game_id: str,
    target_status: str | None,
    target_classes: list[str],
) -> str:
    """Why a bridge would be refused (``""`` when it is allowed).

    Shared by the `bridge_item` write path and the `can_bridge` view, so a
    client can never be told "eligible" by a view that disagrees with the
    transaction that actually runs."""
    if item_status not in ITEM_STATUSES:
        return "unknown item status"
    if item_status != "recognized":
        return "item must be recognized before it can be bridged"
    if target_status is None:
        return "unknown target game"
    if home_game == target_game_id:
        return "item is already in this game"
    if target_status != "active":
        return "target game is not active"
    if item_class not in target_classes:
        return "target game does not accept this item class"
    return ""


def _strip_code_fence(raw: str) -> str:
    """Strip a markdown code fence from LLM JSON output if present."""
    s = raw.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1:] if first_newline != -1 else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _parse_json_object(raw) -> dict | None:
    """Parse an agreed consensus payload, tolerating a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return None


def _to_int(value, lo: int, hi: int) -> int | None:
    """Coerce an LLM-produced number into an int inside ``[lo, hi]``, or None.

    Integer math only: GenVM calldata has no float type, so values are kept
    as integers end-to-end. ``"85.5"`` is sliced at the decimal point and
    ``float()`` is never invoked. Bools are rejected (``bool`` is an ``int``
    subclass in Python)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        num = value
    elif isinstance(value, str):
        text = value.strip()
        if "." in text:
            text = text.split(".")[0]
        if not text or text in ("-", "+"):
            return None
        try:
            num = int(text)
        except ValueError:
            return None
    elif isinstance(value, float):
        text = repr(value)
        if "." in text:
            text = text.split(".")[0]
        try:
            num = int(text)
        except ValueError:
            return None
    else:
        return None
    return num if lo <= num <= hi else None


def _within_tolerance(a: int, b: int, tolerance: int) -> bool:
    return abs(a - b) <= tolerance


# --- Consensus block (leader + validator) -------------------------------------


def _consensus_ok(data, item_spec_url: str, game_spec_url: str) -> bool:
    """Deterministic shape + binding check on the agreed consensus payload.

    Beyond the field types it proves that the observation is bound to the two
    URLs this transaction actually fetched (``sources`` must match storage
    exactly) and that the reachability count is a real fetch count, not a
    model claim. Runs on the returned leader result, so a malformed or
    under-evidenced result can never be stored even if a validator accepted
    it."""
    payload = _parse_json_object(data)
    if payload is None:
        return False
    if not isinstance(payload.get("matched"), bool):
        return False
    item_class = payload.get("item_class")
    if not isinstance(item_class, str) or not item_class.strip():
        return False
    if item_class.strip() not in ITEM_CLASSES:
        return False
    if payload.get("rarity") not in RARITIES:
        return False
    if _to_int(payload.get("power"), 1, 100) is None:
        return False
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        return False
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) != 2:
        return False
    if str(sources[0]) != item_spec_url or str(sources[1]) != game_spec_url:
        return False
    reachable = payload.get("reachable")
    if isinstance(reachable, bool) or not isinstance(reachable, int):
        return False
    if reachable < MIN_REACHABLE_SOURCES or reachable > len(sources):
        return False
    return True


def _consensus_leader(
    token_id: str,
    game_name: str,
    item_spec_url: str,
    game_spec_url: str,
    ecosystem_name: str,
) -> dict:
    """Runs independently on every validator. Fetches the item page and the
    game catalog, and asks the model what the game *published* for this token.

    The prompt deliberately does NOT contain the minted attributes: the model
    has to read the spec, which is the only way an "agreement" between
    validators means anything.

    Returns ``{"matched": bool, "item_class": str, "rarity": str,
    "power": int, "reasoning": str, "reachable": int, "sources": list}``.

    ``reachable`` and ``sources`` are filled in by the contract, not by the
    model: ``reachable`` counts the fetches that actually succeeded and
    ``sources`` pins the two URLs that were actually read, so neither can be
    embellished by the prompt."""
    blocks: list[str] = []
    reachable = 0
    for label, url in (("ITEM", item_spec_url), ("GAME", game_spec_url)):
        try:
            response = gl.nondet.web.get(url)
            text = response.body.decode("utf-8")[:MAX_SPEC_CHARS]
            reachable += 1
        except Exception:
            text = "(specification source unreachable)"
        blocks.append(f"{label} SPECIFICATION {url}\n{text}")

    class_list = ", ".join(ITEM_CLASSES)
    rarity_list = ", ".join(RARITIES)
    prompt = f"""You are the NexusPlay item recognition auditor for the "{ecosystem_name}" ecosystem.
Your job is to recognize an auto-generated NFT item by reading what its game
published for it. Report ONLY what the sources below say; never guess.

Token: {token_id}
Game: {game_name}

SPECIFICATIONS:
{"\n\n".join(blocks)}

Set "matched" to true only when the sources clearly document this token as an
in-game item with coherent attributes. Set "item_class" to one of: {class_list}.
Set "rarity" to one of: {rarity_list}.
Set "power" to the integer from 1 to 100 that the sources state for this item.
If neither source states a power, set "power" to 0 -- never invent a value.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"matched": <true|false>, "item_class": "<class>", "rarity": "<rarity>", "power": <0-100>, "reasoning": "<2-3 sentences>"}}"""

    parsed = gl.nondet.exec_prompt(prompt, response_format="json")
    payload = _parse_json_object(parsed) or {}
    power = _to_int(payload.get("power"), 1, 100)
    return {
        "matched": bool(payload.get("matched", False)),
        "item_class": str(payload.get("item_class", "")).strip(),
        "rarity": str(payload.get("rarity", "")),
        "power": int(power) if power is not None else 0,
        "reasoning": str(payload.get("reasoning", "")),
        "reachable": reachable,
        "sources": [item_spec_url, game_spec_url],
    }


def _consensus_validator(
    leaders_res,
    token_id: str,
    game_name: str,
    item_spec_url: str,
    game_spec_url: str,
    ecosystem_name: str,
) -> bool:
    """The equivalence check. Runs on every validator, which independently
    re-fetches both sources and re-reads them. The leader's result is accepted
    only if:

    - the leader result passes the deterministic shape/binding check;
    - `matched` agrees exactly;
    - `item_class` agrees exactly;
    - `rarity` agrees exactly;
    - `power` agrees EXACTLY -- both validators read the same two pages, so
      any tolerance here would let two different published readings pass
      consensus and store an observation nobody can reproduce;
    - the deterministic reachable-source count agrees exactly;
    - both sides report the same pinned source URLs.

    The `reasoning` text is deliberately NOT compared: two good readers will
    word it differently, and it never reaches storage decisions."""
    if not isinstance(leaders_res, gl.vm.Return):
        return False
    leader_data = leaders_res.calldata
    if not isinstance(leader_data, dict):
        return False
    if not _consensus_ok(leader_data, item_spec_url, game_spec_url):
        return False

    my_data = _consensus_leader(
        token_id, game_name, item_spec_url, game_spec_url, ecosystem_name
    )
    if not _consensus_ok(my_data, item_spec_url, game_spec_url):
        return False

    if int(leader_data["reachable"]) != int(my_data["reachable"]):
        return False
    if bool(leader_data["matched"]) != bool(my_data["matched"]):
        return False
    if str(leader_data["item_class"]).strip() != str(my_data["item_class"]).strip():
        return False
    if str(leader_data["rarity"]) != str(my_data["rarity"]):
        return False
    leader_power = _to_int(leader_data.get("power"), 1, 100)
    my_power = _to_int(my_data.get("power"), 1, 100)
    if leader_power is None or my_power is None:
        return False
    return int(leader_power) == int(my_power)


# --- Storage models ------------------------------------------------------------


@allow_storage
@dataclass
class Game:
    game_id: str
    name: str
    publisher: Address
    status: str                      # "active" | "paused"
    spec_url: str                    # published catalog the recognition reads
    item_classes: DynArray[str]      # classes this game may issue
    item_count: u256
    created_at: u256

    def as_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "name": self.name,
            "publisher": str(self.publisher),
            "status": self.status,
            "spec_url": self.spec_url,
            "item_classes": [c for c in self.item_classes],
            "item_count": int(self.item_count),
            "created_at": int(self.created_at),
        }


@allow_storage
@dataclass
class Item:
    token_id: str
    origin_game: str                 # game that issued it
    home_game: str                   # game it currently lives in
    owner: Address
    item_class: str                  # ecosystem taxonomy class
    rarity: str
    power: u256
    seed: str                        # public derivation input
    spec_url: str                    # published item page recognition reads
    status: str                      # "issued" | "recognized"
    minted_at: u256
    recognized_at: u256
    bridge_count: u256

    def as_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "origin_game": self.origin_game,
            "home_game": self.home_game,
            "owner": str(self.owner),
            "item_class": self.item_class,
            "rarity": self.rarity,
            "power": int(self.power),
            "seed": self.seed,
            "spec_url": self.spec_url,
            "status": self.status,
            "minted_at": int(self.minted_at),
            "recognized_at": int(self.recognized_at),
            "bridge_count": int(self.bridge_count),
        }


@allow_storage
@dataclass
class Recognition:
    token_id: str
    spec_url: str
    matched: bool
    item_class: str
    rarity: str
    power: u256           # recognized value == the minted value (invariant)
    reported_power: u256  # what consensus actually read off the page (audit)
    reasoning: str
    recognized_at: u256

    def as_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "spec_url": self.spec_url,
            "matched": self.matched,
            "item_class": self.item_class,
            "rarity": self.rarity,
            "power": int(self.power),
            "reported_power": int(self.reported_power),
            "reasoning": self.reasoning,
            "recognized_at": int(self.recognized_at),
        }


@allow_storage
@dataclass
class OwnerIndex:
    tokens: DynArray[str]

    def as_dict(self) -> dict:
        return {"tokens": [t for t in self.tokens]}


def _index_add(index: OwnerIndex | None, token_id: str) -> OwnerIndex:
    """Add a token to an owner's index (creates the index when missing)."""
    tokens = [] if index is None else [t for t in index.tokens]
    if token_id not in tokens:
        tokens.append(token_id)
    return OwnerIndex(tokens=tokens)


def _index_remove(index: OwnerIndex | None, token_id: str) -> OwnerIndex:
    """Remove a token from an owner's index (tolerates a missing index)."""
    tokens = [] if index is None else [t for t in index.tokens]
    return OwnerIndex(tokens=[t for t in tokens if t != token_id])


# --- The contract --------------------------------------------------------------


class NexusPlay(gl.Contract):
    ecosystem_name: str
    deployer: Address
    games: TreeMap[str, Game]
    items: TreeMap[str, Item]
    recognitions: TreeMap[str, Recognition]
    owner_index: TreeMap[str, OwnerIndex]
    used_seeds: TreeMap[str, bool]
    game_count: u256
    item_count: u256

    def __init__(self, ecosystem_name: str):
        self.ecosystem_name = _validate_text(
            ecosystem_name, "ecosystem_name", MAX_ECOSYSTEM_NAME
        )
        self.deployer = gl.message.sender_address
        self.game_count = u256(0)
        self.item_count = u256(0)

    # -- Game registry --------------------------------------------------------

    @gl.public.write
    def register_game(
        self, game_id, name, spec_url, item_classes: list[str]
    ) -> str:
        """Register a game in the ecosystem. The caller becomes its publisher:
        the only address that may mint items for it or pause it. Returns the
        game_id."""
        game_id = _validate_game_id(game_id)
        name = _validate_text(name, "name", MAX_TITLE)
        spec_url = _validate_url(spec_url)
        classes = _validate_classes(item_classes)

        if game_id in self.games:
            raise gl.vm.UserError("game_id already registered")

        self.games[game_id] = Game(
            game_id=game_id,
            name=name,
            publisher=gl.message.sender_address,
            status="active",
            spec_url=spec_url,
            item_classes=classes,
            item_count=u256(0),
            created_at=_current_timestamp(),
        )
        self.game_count = self.game_count + u256(1)
        return game_id

    @gl.public.write
    def set_game_status(self, game_id, status) -> None:
        """Pause or resume a game. Publisher only. A paused game cannot mint
        or receive bridges."""
        game_id = _validate_game_id(game_id)
        status = str(status).strip()
        if status not in GAME_STATUSES:
            raise gl.vm.UserError(f"status must be one of {GAME_STATUSES}")

        game = self.games.get(game_id)
        if game is None:
            raise gl.vm.UserError("unknown game_id")
        if gl.message.sender_address != game.publisher:
            raise gl.vm.UserError("only the game publisher can change status")

        game.status = status
        self.games[game_id] = game

    # -- Minting (auto-generated items) ---------------------------------------

    @gl.public.write
    def mint_item(self, game_id, recipient, seed, spec_url) -> str:
        """Issue one auto-generated NFT item to `recipient`.

        Every attribute is derived from ``game_id:seed:index`` -- nothing is
        chosen by an AI or by the publisher at mint time. Only the game's
        publisher can mint, only while the game is active, and each
        (game, seed) pair may be used once. Returns the new token_id."""
        game_id = _validate_game_id(game_id)
        seed = _validate_seed(seed)
        spec_url = _validate_url(spec_url)

        game = self.games.get(game_id)
        if game is None:
            raise gl.vm.UserError("unknown game_id")
        if game.status != "active":
            raise gl.vm.UserError("game is not active")
        if gl.message.sender_address != game.publisher:
            raise gl.vm.UserError("only the game publisher can mint")

        recipient_addr = _coerce_address(recipient)
        if _is_zero_address(recipient_addr):
            raise gl.vm.UserError("recipient must not be the zero address")

        if self.used_seeds.get(_seed_key(game_id, seed), False):
            raise gl.vm.UserError("seed already minted by this game")

        classes = [c for c in game.item_classes]
        item_key = f"{game_id}:{seed}:{int(game.item_count)}"
        item_class, rarity, power = _derive_attributes(item_key, classes)

        token_id = f"item-{int(self.item_count)}"
        self.used_seeds[_seed_key(game_id, seed)] = True
        self.items[token_id] = Item(
            token_id=token_id,
            origin_game=game_id,
            home_game=game_id,
            owner=recipient_addr,
            item_class=item_class,
            rarity=rarity,
            power=u256(power),
            seed=seed,
            spec_url=spec_url,
            status="issued",
            minted_at=_current_timestamp(),
            recognized_at=u256(0),
            bridge_count=u256(0),
        )
        self.owner_index[str(recipient_addr)] = _index_add(
            self.owner_index.get(str(recipient_addr)), token_id
        )

        game.item_count = game.item_count + u256(1)
        self.games[game_id] = game
        self.item_count = self.item_count + u256(1)
        return token_id

    # -- Recognition (consensus) ----------------------------------------------

    @gl.public.write
    def recognize_item(self, token_id) -> dict:
        """Run consensus recognition of an issued item. Anyone may trigger it.

        Validators re-read the item page and the game catalog; the result is
        stored only when the agreed class/rarity match what the contract
        derived at mint, power is within tolerance, and both sources were
        reachable. Returns the recognition dict."""
        token_id = str(token_id)
        item = self.items.get(token_id)
        if item is None:
            raise gl.vm.UserError("unknown token_id")
        if item.status != "issued":
            raise gl.vm.UserError("item is already recognized")

        game = self.games.get(item.origin_game)
        if game is None:
            raise gl.vm.UserError("origin game is missing")

        game_name = game.name
        item_spec_url = item.spec_url
        game_spec_url = game.spec_url
        ecosystem_name = self.ecosystem_name

        # The non-deterministic block never touches storage -- it only fetches
        # the two published sources and proposes an answer. Named nested
        # functions (not lambdas) keep GenVM lint's call-graph analysis from
        # reaching the deterministic side effects below.
        def leader_fn():
            return _consensus_leader(
                token_id, game_name, item_spec_url, game_spec_url, ecosystem_name
            )

        def validator_fn(leaders_res):
            return _consensus_validator(
                leaders_res,
                token_id, game_name, item_spec_url, game_spec_url,
                ecosystem_name,
            )

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        if not isinstance(result, dict):
            raise gl.vm.UserError("consensus result was unusable")

        reachable = result.get("reachable", 0)
        if isinstance(reachable, bool) or not isinstance(reachable, int):
            reachable = 0
        if int(reachable) < MIN_REACHABLE_SOURCES:
            raise gl.vm.UserError("both specification sources must be reachable")
        if not _consensus_ok(result, item_spec_url, game_spec_url):
            raise gl.vm.UserError("consensus result was unusable")
        if not result["matched"]:
            raise gl.vm.UserError("published specification does not confirm this item")

        canonical_class = str(result["item_class"]).strip()
        canonical_rarity = str(result["rarity"])
        reported_power = _to_int(result["power"], 1, 100)
        if reported_power is None:
            raise gl.vm.UserError("consensus returned an out-of-range power")

        # Deterministic backstop: consensus confirms the minted item, it can
        # never rewrite it. The values that get stored below are the minted
        # ones, so `get_recognition()` can never disagree with `get_item()`.
        if canonical_class not in ITEM_CLASSES:
            raise gl.vm.UserError("canonical class is outside the ecosystem taxonomy")
        if canonical_class != item.item_class:
            raise gl.vm.UserError("canonical class does not match the minted item")
        if canonical_rarity != item.rarity:
            raise gl.vm.UserError("canonical rarity does not match the minted item")
        if not _within_tolerance(reported_power, int(item.power), POWER_TOLERANCE):
            raise gl.vm.UserError("published power deviates from the minted item")

        now = _current_timestamp()
        item.status = "recognized"
        item.recognized_at = now
        self.items[token_id] = item

        self.recognitions[token_id] = Recognition(
            token_id=token_id,
            spec_url=item_spec_url,
            matched=True,
            item_class=item.item_class,
            rarity=item.rarity,
            power=item.power,
            reported_power=u256(reported_power),
            reasoning=str(result["reasoning"]),
            recognized_at=now,
        )

        return {
            "token_id": token_id,
            "item_class": item.item_class,
            "rarity": item.rarity,
            "power": int(item.power),
            "reported_power": reported_power,
            "matched": True,
            "reachable": int(result["reachable"]),
            "reasoning": str(result["reasoning"]),
        }

    @gl.public.write
    def update_item_spec_url(self, token_id, new_spec_url) -> None:
        """Repair a dead or wrong item page before recognition.

        Only the publisher of the item's *origin* game may do it, and only
        while the item is still ``issued``: once recognition has run, the
        evidence URL is frozen, so an already-confirmed item can never be
        re-pointed at a different page (evidence replacement)."""
        token_id = str(token_id)
        new_spec_url = _validate_url(new_spec_url)

        item = self.items.get(token_id)
        if item is None:
            raise gl.vm.UserError("unknown token_id")
        if item.status != "issued":
            raise gl.vm.UserError("item evidence is frozen after recognition")

        game = self.games.get(item.origin_game)
        if game is None:
            raise gl.vm.UserError("origin game is missing")
        if gl.message.sender_address != game.publisher:
            raise gl.vm.UserError(
                "only the origin game publisher can update the spec URL"
            )

        item.spec_url = new_spec_url
        self.items[token_id] = item

    # -- Movement between players and games -----------------------------------

    @gl.public.write
    def transfer_item(self, token_id, recipient) -> None:
        """Player-to-player transfer. Owner only; the owner index of both
        sides is updated atomically with the ownership change."""
        token_id = str(token_id)
        item = self.items.get(token_id)
        if item is None:
            raise gl.vm.UserError("unknown token_id")
        if item.owner != gl.message.sender_address:
            raise gl.vm.UserError("only the owner can transfer")

        recipient_addr = _coerce_address(recipient)
        if _is_zero_address(recipient_addr):
            raise gl.vm.UserError("recipient must not be the zero address")
        if recipient_addr == item.owner:
            raise gl.vm.UserError("recipient is already the owner")

        old_key = str(item.owner)
        new_key = str(recipient_addr)
        self.owner_index[old_key] = _index_remove(
            self.owner_index.get(old_key), token_id
        )
        self.owner_index[new_key] = _index_add(
            self.owner_index.get(new_key), token_id
        )

        item.owner = recipient_addr
        self.items[token_id] = item

    @gl.public.write
    def bridge_item(self, token_id, target_game_id) -> dict:
        """Move a recognized item into another active game that accepts its
        class. The token id and the owner are unchanged -- the player's asset
        simply starts living in a different game of the same ecosystem."""
        token_id = str(token_id)
        target_game_id = _validate_game_id(target_game_id)

        item = self.items.get(token_id)
        if item is None:
            raise gl.vm.UserError("unknown token_id")
        if item.owner != gl.message.sender_address:
            raise gl.vm.UserError("only the owner can bridge")

        target = self.games.get(target_game_id)
        blocker = _bridge_blocker(
            item.status,
            item.home_game,
            item.item_class,
            target_game_id,
            None if target is None else target.status,
            [] if target is None else [c for c in target.item_classes],
        )
        if blocker:
            raise gl.vm.UserError(blocker)

        item.home_game = target_game_id
        item.bridge_count = item.bridge_count + u256(1)
        self.items[token_id] = item

        return {
            "token_id": token_id,
            "home_game": target_game_id,
            "owner": str(item.owner),
            "item_class": item.item_class,
            "bridge_count": int(item.bridge_count),
        }

    # -- Views ----------------------------------------------------------------

    @gl.public.view
    def get_ecosystem(self) -> dict:
        return {
            "ecosystem_name": self.ecosystem_name,
            "deployer": str(self.deployer),
            "game_count": int(self.game_count),
            "item_count": int(self.item_count),
            "item_classes": list(ITEM_CLASSES),
            "rarities": list(RARITIES),
        }

    @gl.public.view
    def get_game(self, game_id) -> dict:
        game_id = _validate_game_id(game_id)
        game = self.games.get(game_id)
        if game is None:
            raise gl.vm.UserError("unknown game_id")
        return game.as_dict()

    @gl.public.view
    def get_item(self, token_id) -> dict:
        token_id = str(token_id)
        item = self.items.get(token_id)
        if item is None:
            raise gl.vm.UserError("unknown token_id")
        return item.as_dict()

    @gl.public.view
    def get_recognition(self, token_id) -> dict:
        token_id = str(token_id)
        recognition = self.recognitions.get(token_id)
        if recognition is None:
            raise gl.vm.UserError("no recognition for this item")
        return recognition.as_dict()

    @gl.public.view
    def owner_of(self, token_id) -> str:
        token_id = str(token_id)
        item = self.items.get(token_id)
        if item is None:
            raise gl.vm.UserError("unknown token_id")
        return str(item.owner)

    @gl.public.view
    def get_owner_items(self, owner) -> list[str]:
        key = str(_coerce_address(owner))
        index = self.owner_index.get(key)
        return [] if index is None else [t for t in index.tokens]

    @gl.public.view
    def get_owner_items_page(self, owner, offset, limit) -> list[str]:
        """Bounded page of an owner's tokens, for wallets with large
        inventories. ``offset``/``limit`` are clamped to sane ranges so a
        hostile caller can never force an unbounded scan."""
        start = int(offset)
        if start < 0:
            start = 0
        size = int(limit)
        if size < 0:
            size = 0
        if size > MAX_PAGE:
            size = MAX_PAGE
        tokens = self.get_owner_items(owner)
        end = start + size
        return [t for t in tokens[start:end]]

    @gl.public.view
    def get_owner_item_count(self, owner) -> int:
        return len(self.get_owner_items(owner))

    @gl.public.view
    def get_game_count(self) -> u256:
        return self.game_count

    @gl.public.view
    def get_item_count(self) -> u256:
        return self.item_count

    @gl.public.view
    def can_bridge(self, token_id, target_game_id) -> dict:
        """Eligibility report for a bridge, evaluated with the exact same
        helper the transaction uses. Ownership is not checked here (it is a
        property of the caller, not of the asset)."""
        token_id = str(token_id)
        target_game_id = str(target_game_id)
        item = self.items.get(token_id)
        if item is None:
            return {"eligible": False, "reason": "unknown token_id"}

        target = self.games.get(target_game_id)
        blocker = _bridge_blocker(
            item.status,
            item.home_game,
            item.item_class,
            target_game_id,
            None if target is None else target.status,
            [] if target is None else [c for c in target.item_classes],
        )
        return {"eligible": blocker == "", "reason": blocker}
