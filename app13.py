import os
import secrets
import sqlite3
import random
import time
import json
from functools import wraps
from urllib.parse import urlencode, quote

import requests
from flask import (
    Flask, request, redirect, session, jsonify,
    render_template_string, url_for
)

# ============================================================
# PetSoul / ColorDice - Single File Pet Simulator 99 Item Dice Site
# ============================================================
# This version uses virtual Gems and a 100% website-side Pet Simulator 99 item inventory.
# Big Games API is used only for public catalog/RAP metadata; it never reads or transfers player inventory.
#
# Install:
#   pip install flask requests
#
# Discord OAuth2 environment variables:
#   DISCORD_CLIENT_ID
#   DISCORD_CLIENT_SECRET
#   DISCORD_REDIRECT_URI
#
# Example local redirect URI:
#   http://127.0.0.1:5000/callback
#
# Run:
#   python app.py
# ============================================================

app = Flask(__name__)

# Keep the Flask session key stable across restarts.
# Without this, a freshly generated key invalidates the OAuth session
# whenever the server restarts while Discord is still redirecting back.
SESSION_SECRET_FILE = ".colordice_session_secret"

try:
    if os.path.exists(SESSION_SECRET_FILE):
        with open(SESSION_SECRET_FILE, "r", encoding="utf-8") as f:
            _session_secret = f.read().strip()
    else:
        _session_secret = secrets.token_hex(32)
        with open(SESSION_SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(_session_secret)
except Exception:
    _session_secret = os.getenv(
        "FLASK_SECRET_KEY",
        "colordice-local-session-key"
    )

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    _session_secret
)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
)

DB_FILE = "colordice.db"
START_BALANCE = 1000.00
MAX_BET = 500_000_000.00

# Pet Simulator 99 public data. The legacy /api/* endpoints are
# public and provide item/pet configuration, images, Exists and RAP.
# Public player inventory is available through /v1/players/:username
# when that Roblox account has enabled its public inventory view.
BIG_GAMES_API = "https://ps99.biggamesapi.io"
GIVE_ITEM_CODE = os.getenv("GIVE_ITEM_CODE", "").strip()
if not GIVE_ITEM_CODE:
    _give_code_file = ".colordice_give_code"
    try:
        if os.path.exists(_give_code_file):
            GIVE_ITEM_CODE = open(_give_code_file, "r", encoding="utf-8").read().strip()
        if not GIVE_ITEM_CODE:
            GIVE_ITEM_CODE = secrets.token_urlsafe(8)[:10]
            with open(_give_code_file, "w", encoding="utf-8") as _f:
                _f.write(GIVE_ITEM_CODE)
    except Exception:
        GIVE_ITEM_CODE = secrets.token_urlsafe(8)[:10]

DISCORD_API = "https://discord.com/api/v10"


DISCORD_CLIENT_ID = os.getenv(
    "DISCORD_CLIENT_ID",
    "1559551889404723302"
)

DISCORD_CLIENT_SECRET = os.getenv(
    "DISCORD_CLIENT_SECRET",
    "f7jYBO9xtzvMPi3tQomv4QgvqElW4YnT"
)

DISCORD_REDIRECT_URI = os.getenv(
    "DISCORD_REDIRECT_URI",
    "https://my-website-production-71a9.up.railway.app/callback"
)

DISCORD_API = "https://discord.com/api/v10"

# ------------------------------------------------------------
# Colors
# ------------------------------------------------------------

COLORS = {
    "red": "#ef3340",
    "blue": "#3b82f6",
    "green": "#16b364",
    "purple": "#963cf2",
    "orange": "#f97316",
    "yellow": "#f5c542",
}

COLOR_NAMES = list(COLORS.keys())


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_db():

    conn = db()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id TEXT UNIQUE,
        username TEXT NOT NULL,
        display_name TEXT NOT NULL,
        avatar TEXT,
        balance REAL NOT NULL DEFAULT 1000,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        games INTEGER NOT NULL DEFAULT 0,
        total_wager REAL NOT NULL DEFAULT 0,
        roblox_username TEXT,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        source_key TEXT NOT NULL,
        item_class TEXT NOT NULL DEFAULT 'Pet',
        item_id TEXT NOT NULL,
        name TEXT NOT NULL,
        variant TEXT,
        icon_url TEXT,
        value REAL NOT NULL DEFAULT 0,
        count INTEGER NOT NULL DEFAULT 1,
        source_count INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(owner_id, source_key)
    );

    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,

        creator_id INTEGER NOT NULL,

        opponent_id INTEGER,

        bet REAL NOT NULL,

        creator_color TEXT NOT NULL,

        opponent_color TEXT,

        status TEXT NOT NULL DEFAULT 'waiting',

        winner_id INTEGER,

        rolls TEXT,

        created_at INTEGER NOT NULL,

        finished_at INTEGER,

        animation_started_at INTEGER,
        match_mode TEXT NOT NULL DEFAULT '1v1',
        third_player_id INTEGER,
        third_color TEXT,
        third_is_bot INTEGER NOT NULL DEFAULT 0,
        best_of INTEGER NOT NULL DEFAULT 1,
        series_wins TEXT NOT NULL DEFAULT '{}',
        creator_items TEXT NOT NULL DEFAULT '[]',
        opponent_items TEXT NOT NULL DEFAULT '[]',
        third_items TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS chat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        message TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS oauth_states (
        state TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_matches_status
        ON matches(status);

    CREATE INDEX IF NOT EXISTS idx_chat_id
        ON chat(id);
    """)

    # --------------------------------------------------------
    # Migration for old database
    # --------------------------------------------------------

    columns = [
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(matches)"
        ).fetchall()
    ]

    if "opponent_is_bot" not in columns:
        conn.execute("""
            ALTER TABLE matches
            ADD COLUMN opponent_is_bot INTEGER DEFAULT 0
        """)

    user_columns = [
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(users)"
        ).fetchall()
    ]

    if "total_wager" not in user_columns:
        conn.execute("""
            ALTER TABLE users
            ADD COLUMN total_wager REAL NOT NULL DEFAULT 0
        """)

        conn.execute("""
            UPDATE users
            SET total_wager = COALESCE((
                SELECT SUM(m.bet)
                FROM matches m
                WHERE m.creator_id = users.id
                AND m.status IN ('ready', 'finished')
            ), 0)
            + COALESCE((
                SELECT SUM(m.bet)
                FROM matches m
                WHERE m.opponent_id = users.id
                AND m.status IN ('ready', 'finished')
            ), 0)
        """)

    if "roblox_username" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN roblox_username TEXT")

    match_columns = [
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(matches)"
        ).fetchall()
    ]

    if "animation_started_at" not in match_columns:
        conn.execute("""
            ALTER TABLE matches
            ADD COLUMN animation_started_at INTEGER
        """)

    for _col, _definition in (
        ("match_mode", "TEXT NOT NULL DEFAULT '1v1'"),
        ("third_player_id", "INTEGER"),
        ("third_color", "TEXT"),
        ("third_is_bot", "INTEGER NOT NULL DEFAULT 0"),
        ("best_of", "INTEGER NOT NULL DEFAULT 1"),
        ("series_wins", "TEXT NOT NULL DEFAULT '{}'"),
        ("creator_items", "TEXT NOT NULL DEFAULT '[]'"),
        ("opponent_items", "TEXT NOT NULL DEFAULT '[]'"),
        ("third_items", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if _col not in match_columns:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {_col} {_definition}")

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def money(value):
    return "$" + format(float(value), ",.2f")


def gem_value(value):
    """Compact PS99-style value: K / M / B / T."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    units = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))
    for threshold, suffix in units:
        if n >= threshold:
            v = n / threshold
            text = f"{v:.2f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{int(n) if n.is_integer() else round(n, 2)}"


# Small in-process cache. Big Games recommends caching public API data and
# rate-limits callers, so inventory/value refreshes are deliberately throttled.
_BG_CACHE = {}
_BG_CACHE_TTL = 300

def bg_get(path, ttl=_BG_CACHE_TTL):
    now = time.time()
    cached = _BG_CACHE.get(path)
    if cached and now - cached[0] < ttl:
        return cached[1]
    response = requests.get(f"{BIG_GAMES_API}{path}", timeout=20)
    response.raise_for_status()
    data = response.json()
    _BG_CACHE[path] = (now, data)
    return data


def icon_from_rbxasset(asset):
    if not asset:
        return ""
    text = str(asset)
    if text.startswith("rbxassetid://"):
        asset_id = text.split("//", 1)[1]
        if asset_id.isdigit():
            return f"{BIG_GAMES_API}/image/{asset_id}"
    if text.isdigit():
        return f"{BIG_GAMES_API}/image/{text}"
    return text


def item_variant(raw):
    raw = raw or {}
    parts = []
    if raw.get("pt") == 1:
        parts.append("Golden")
    elif raw.get("pt") == 2:
        parts.append("Rainbow")
    if raw.get("sh"):
        parts.append("Shiny")
    if raw.get("tn") is not None:
        parts.append(f"Tier {raw.get('tn')}")
    return " ".join(parts)


def compact_item_snapshot(row):
    return {
        "id": int(row["id"]),
        "source_key": row["source_key"],
        "item_class": row["item_class"],
        "item_id": row["item_id"],
        "name": row["name"],
        "variant": row["variant"] or "",
        "icon": row["icon_url"] or "",
        "value": float(row["value"] or 0),
        "count": int(row["count"] or 1),
        "virtual": True,
    }


def parse_item_snapshots(value):
    if not value:
        return []
    try:
        data = json.loads(value) if isinstance(value, str) else value
        return data if isinstance(data, list) else []
    except Exception:
        return []


def item_snapshots_value(items):
    return sum(float(x.get("value", 0)) * int(x.get("count", 1)) for x in (items or []))


def grant_item_snapshots(conn, owner_id, snapshots):
    now = int(time.time())
    for item in snapshots or []:
        # All inventory items are website-only virtual items.
        # They never represent or transfer real Roblox inventory.
        source_key = str(item.get("source_key") or f"virtual-transfer:{secrets.token_hex(12)}")
        existing = conn.execute(
            "SELECT * FROM inventory_items WHERE owner_id=? AND source_key=?",
            (owner_id, source_key)
        ).fetchone()
        count = max(1, int(item.get("count", 1)))
        if existing:
            conn.execute(
                "UPDATE inventory_items SET count=count+?, value=?, icon_url=?, name=?, variant=?, updated_at=? WHERE id=?",
                (count, float(item.get("value", 0)), item.get("icon", ""), item.get("name", "Item"), item.get("variant", ""), now, existing["id"])
            )
        else:
            conn.execute(
                """INSERT INTO inventory_items
                   (owner_id,source_key,item_class,item_id,name,variant,icon_url,value,count,source_count,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner_id, source_key, item.get("item_class", "Pet"), item.get("item_id", item.get("name", "Item")), item.get("name", "Item"), item.get("variant", ""), item.get("icon", ""), float(item.get("value", 0)), count, 0, now, now)
            )


def sync_inventory_for_user(user_id):
    """Compatibility shim for the old sync endpoint.

    ColorDice inventory is now 100% virtual and stored in this site's SQLite DB.
    Big Games API is used only for public catalog/value metadata, never for a
    player's Roblox inventory.
    """
    return True, "This is a virtual website inventory. Use Deposit Items to add pets."


def _catalog_records(payload):
    """Normalize several Big Games collection response shapes into a list."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "collection", "pets", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _catalog_item_value(rap_map, category, item_id, raw=None):
    raw = raw or {}
    candidates = [
        (category, item_id, str(raw.get("pt", "")), str(raw.get("sh", "")), str(raw.get("tn", ""))),
        (category, item_id, "", "", ""),
        ("Pet", item_id, "", "", ""),
    ]
    for key in candidates:
        if key in rap_map:
            return float(rap_map[key] or 0)
    return 0.0


def virtual_catalog(limit=5000):
    """Read-only PS99 catalog metadata. Never reads a player's inventory."""
    try:
        pets_payload = bg_get("/api/collection/Pets", ttl=600)
    except Exception:
        pets_payload = {"data": []}
    try:
        rap_payload = bg_get("/api/rap", ttl=300)
    except Exception:
        rap_payload = {"data": []}

    rap_map = {}
    for r in rap_payload.get("data", []) if isinstance(rap_payload, dict) else []:
        cd = r.get("configData") or {}
        key = (
            str(r.get("category", "")),
            str(cd.get("id", "")),
            str(cd.get("pt", "")),
            str(cd.get("sh", "")),
            str(cd.get("tn", "")),
        )
        rap_map[key] = float(r.get("value") or 0)

    output = []
    seen = set()
    for raw in _catalog_records(pets_payload):
        if not isinstance(raw, dict):
            continue
        config = raw.get("configData") or raw.get("config") or {}

        # BIG Games' /api/collection/Pets response uses:
        #   configName: "Huge Cosmic Axolotl"
        #   configData: { name: "Huge Cosmic Axolotl", thumbnail: "rbxassetid://..." }
        # Older code only looked for raw.id/raw.name/config.id, which made
        # every catalog entry get an empty item_id and therefore disappear.
        item_id = str(
            raw.get("id")
            or raw.get("name")
            or raw.get("configName")
            or config.get("id")
            or config.get("name")
            or ""
        ).strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)

        name = str(
            raw.get("displayName")
            or raw.get("name")
            or raw.get("configName")
            or config.get("name")
            or item_id
        )

        icon = (
            raw.get("thumbnail")
            or raw.get("thumbnailUrl")
            or raw.get("icon")
            or raw.get("iconUrl")
            or raw.get("image")
            or config.get("thumbnail")
            or config.get("icon")
            or config.get("iconUrl")
            or config.get("image")
            or ""
        )
        if isinstance(icon, dict):
            icon = icon.get("url") or icon.get("assetId") or icon.get("id") or ""
        icon = icon_from_rbxasset(icon)
        value = _catalog_item_value(rap_map, "Pet", item_id, raw)
        if value <= 0:
            value = float(raw.get("rap") or raw.get("value") or 0)
        output.append({
            "catalog_key": f"Pet:{item_id}",
            "item_class": "Pet",
            "item_id": item_id,
            "name": name,
            "variant": "",
            "icon": icon,
            "value": value,
            "virtual": True,
        })
        if len(output) >= limit:
            break
    output.sort(key=lambda x: (-float(x["value"]), x["name"].lower()))
    return output


def user_level(total_wager):
    """Progressive wager-based levels: early levels are quick, later levels cost more."""
    try:
        wager = max(0.0, float(total_wager or 0))
    except (TypeError, ValueError):
        return 1
    level = 1
    # Cumulative wager required to reach each next level: 10, 30, 60, 100, 150...
    while wager >= 10 * level * (level + 1) / 2:
        level += 1
        if level > 10000:
            break
    return level


def match_animation_duration_ms(roll_history):
    total = 3000
    for rolls in roll_history or []:
        total += 700 + (4 * 240) + 500
    return total


def parse_colors(value):

    if not value:
        return []

    # New format: JSON array
    try:

        parsed = json.loads(value)

        if isinstance(parsed, list):
            return [
                str(x).lower()
                for x in parsed
                if str(x).lower() in COLORS
            ]

    except Exception:
        pass

    # Old format: "red"
    color = str(value).lower()

    if color in COLORS:
        return [color]

    return []


def save_colors(colors):
    return json.dumps(colors)


def choose_bot_colors(player_colors):

    available = [
        c for c in COLOR_NAMES
        if c not in player_colors
    ]

    if len(player_colors) == 1:
        return [random.choice(available)]

    if len(player_colors) == 2:
        return random.sample(
            available,
            min(2, len(available))
        )

    return [random.choice(available)]


def color_score(rolls, selected_colors):

    return sum(
        1
        for color in rolls
        if color in selected_colors
    )


def roll_round():

    return [
        random.choice(COLOR_NAMES)
        for _ in range(4)
    ]


def current_user():

    uid = session.get("user_id")

    if not uid:
        return None

    conn = db()

    user = conn.execute(
        """
        SELECT *
        FROM users
        WHERE id = ?
        """,
        (uid,)
    ).fetchone()

    conn.close()

    return user


def login_required(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        if not current_user():

            return jsonify({
                "ok": False,
                "error": "Login with Discord first."
            }), 401

        return fn(*args, **kwargs)

    return wrapper


def discord_avatar_url(user):

    avatar = user.get("avatar")

    if avatar:
        return (
            f"https://cdn.discordapp.com/avatars/"
            f"{user['id']}/{avatar}.png?size=256"
        )

    try:
        discriminator = int(
            user.get("discriminator", "0")
        )
    except Exception:
        discriminator = 0

    return (
        "https://cdn.discordapp.com/embed/avatars/"
        f"{discriminator % 5}.png"
    )


def public_user(user_id):

    if not user_id:
        return None

    conn = db()

    user = conn.execute(
        """
        SELECT
            id,
            username,
            display_name,
            avatar,
            total_wager
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    ).fetchone()

    conn.close()

    if not user:
        return None

    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "avatar": user["avatar"] or
                  "https://cdn.discordapp.com/embed/avatars/0.png",
        "total_wager": float(user["total_wager"] or 0),
        "level": user_level(user["total_wager"] or 0),
    }


def bot_public_user():

    return {
        "id": -1,
        "username": "ColorDiceBot",
        "display_name": "ColorDice Bot",
        "avatar":
            "https://cdn.discordapp.com/embed/avatars/1.png",
    }


def match_json(row):

    creator = public_user(
        row["creator_id"]
    )

    if row["opponent_is_bot"]:

        opponent = bot_public_user()

    else:

        opponent = public_user(
            row["opponent_id"]
        ) if row["opponent_id"] else None

    roll_data = (
        json.loads(row["rolls"])
        if row["rolls"]
        else []
    )

    # Old database format:
    # ["red", "blue", "green", "yellow"]
    # New format:
    # [["red", "blue", "green", "yellow"], [...]]
    if roll_data and isinstance(roll_data[0], str):
        roll_history = [roll_data]
    else:
        roll_history = roll_data

    final_rolls = (
        roll_history[-1]
        if roll_history
        else []
    )

    creator_items = parse_item_snapshots(row["creator_items"]) if "creator_items" in row.keys() else []
    opponent_items = parse_item_snapshots(row["opponent_items"]) if "opponent_items" in row.keys() else []
    third_items = parse_item_snapshots(row["third_items"]) if "third_items" in row.keys() else []

    return {
        "id": row["id"],

        "creator": creator,

        "opponent": opponent,

        "third_player": (
            ({**bot_public_user(), "id": -2, "username": "ColorDiceBot2", "display_name": "ColorDice Bot 2"} if row["third_is_bot"]
             else public_user(row["third_player_id"]) if row["third_player_id"] else None)
        ),

        "match_mode": row["match_mode"] if "match_mode" in row.keys() else "1v1",

        "best_of": int(row["best_of"] or 1) if "best_of" in row.keys() else 1,

        "series_wins": json.loads(row["series_wins"]) if "series_wins" in row.keys() and row["series_wins"] else {},

        "third_colors": parse_colors(row["third_color"]) if "third_color" in row.keys() else [],

        "bet": float(row["bet"]),

        "creator_items": creator_items,
        "opponent_items": opponent_items,
        "third_items": third_items,
        "creator_item_value": item_snapshots_value(creator_items),
        "opponent_item_value": item_snapshots_value(opponent_items),
        "third_item_value": item_snapshots_value(third_items),
        "total_item_value": item_snapshots_value(creator_items + opponent_items + third_items),

        "creator_colors":
            parse_colors(row["creator_color"]),

        "opponent_colors":
            parse_colors(row["opponent_color"]),

        "status": row["status"],

        "winner_id": row["winner_id"],

        "rolls": final_rolls,

        "roll_history": roll_history,

        "opponent_is_bot":
            bool(row["opponent_is_bot"]),

        "created_at":
            row["created_at"],

        "finished_at":
            row["finished_at"],

        "animation_started_at":
            row["animation_started_at"]
            if "animation_started_at" in row.keys()
            else None,

        "animation_duration_ms":
            match_animation_duration_ms(
                roll_history
            ),
    }


# ============================================================
# AUTH
# ============================================================

@app.route("/login")
def login():

    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:

        return """
        <h2>Discord OAuth is not configured.</h2>

        <p>
        Set:
        DISCORD_CLIENT_ID
        DISCORD_CLIENT_SECRET
        DISCORD_REDIRECT_URI
        </p>

        <p>
        Example:
        http://127.0.0.1:5000/callback
        </p>
        """, 500

    state = secrets.token_urlsafe(32)

    # Store the OAuth state server-side as well as in the Flask session.
    # This prevents a lost/old browser session cookie from causing
    # "Invalid OAuth state" during the Discord redirect.
    conn = db()
    now = int(time.time())

    conn.execute(
        "DELETE FROM oauth_states WHERE created_at < ?",
        (now - 600,)
    )

    conn.execute(
        "INSERT INTO oauth_states (state, created_at) VALUES (?, ?)",
        (state, now)
    )

    conn.commit()
    conn.close()

    session["oauth_state"] = state

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }

    return redirect(
        "https://discord.com/oauth2/authorize?"
        + urlencode(params)
    )


@app.route("/callback")
def callback():

    if request.args.get("error"):
        return redirect(
            "/?error=discord_cancelled"
        )

    state = request.args.get("state")

    if not state:
        return "Invalid OAuth state.", 400

    # Validate the one-time state from the server-side table.
    # We also keep the session comparison when available, but do not
    # reject a valid state merely because an old/missing browser session
    # cookie survived the Discord redirect.
    conn = db()
    state_row = conn.execute(
        "SELECT created_at FROM oauth_states WHERE state = ?",
        (state,)
    ).fetchone()

    if not state_row:
        conn.close()
        return "Invalid OAuth state. Please start Discord login again.", 400

    if int(time.time()) - int(state_row["created_at"]) > 600:
        conn.execute(
            "DELETE FROM oauth_states WHERE state = ?",
            (state,)
        )
        conn.commit()
        conn.close()
        return "OAuth state expired. Please start Discord login again.", 400

    conn.execute(
        "DELETE FROM oauth_states WHERE state = ?",
        (state,)
    )
    conn.commit()
    conn.close()

    code = request.args.get("code")

    if not code:
        return "Missing OAuth code.", 400

    token_response = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri":
                DISCORD_REDIRECT_URI,
        },
        timeout=15,
    )

    if token_response.status_code != 200:
        return (
            "Discord token exchange failed."
        ), 400

    token = token_response.json()

    access_token = token.get(
        "access_token"
    )

    if not access_token:
        return (
            "No Discord access token."
        ), 400

    user_response = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={
            "Authorization":
                f"Bearer {access_token}"
        },
        timeout=15,
    )

    if user_response.status_code != 200:
        return (
            "Discord user lookup failed."
        ), 400

    du = user_response.json()

    discord_id = str(du["id"])

    username = (
        du.get("username")
        or "Discord User"
    )

    display_name = (
        du.get("global_name")
        or username
    )

    avatar = discord_avatar_url(du)

    conn = db()

    existing = conn.execute(
        """
        SELECT *
        FROM users
        WHERE discord_id = ?
        """,
        (discord_id,)
    ).fetchone()

    now = int(time.time())

    if existing:

        conn.execute(
            """
            UPDATE users
            SET
                username = ?,
                display_name = ?,
                avatar = ?
            WHERE discord_id = ?
            """,
            (
                username,
                display_name,
                avatar,
                discord_id,
            )
        )

        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE discord_id = ?
            """,
            (discord_id,)
        ).fetchone()

    else:

        conn.execute(
            """
            INSERT INTO users
            (
                discord_id,
                username,
                display_name,
                avatar,
                balance,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                discord_id,
                username,
                display_name,
                avatar,
                START_BALANCE,
                now,
            )
        )

        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE discord_id = ?
            """,
            (discord_id,)
        ).fetchone()

    conn.commit()
    conn.close()

    session.pop(
        "oauth_state",
        None
    )

    session["user_id"] = user["id"]

    return redirect("/")


@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")


# ============================================================
# API - USER
# ============================================================

@app.route("/api/me")
def api_me():

    user = current_user()

    if not user:

        return jsonify({
            "logged_in": False
        })

    return jsonify({
        "logged_in": True,
        "id": user["id"],
        "username": user["username"],
        "display_name":
            user["display_name"],
        "avatar": user["avatar"],
        "roblox_username": user["roblox_username"] if "roblox_username" in user.keys() else None,
        "balance":
            round(user["balance"], 2),
        "wins": user["wins"],
        "losses": user["losses"],
        "games": user["games"],
        "total_wager": float(
            user["total_wager"] or 0
        ),
        "level": user_level(
            user["total_wager"] or 0
        ),
    })


# ============================================================
# ROBLOX / INVENTORY
# ============================================================

@app.route("/api/roblox", methods=["GET", "POST"])
@login_required
def api_roblox():
    # Kept only so old clients do not break. No Roblox inventory is read.
    if request.method == "POST":
        return jsonify({
            "ok": False,
            "error": "Roblox inventory sync is disabled. ColorDice uses a virtual website inventory."
        }), 410
    return jsonify({
        "ok": True,
        "virtual_inventory": True,
        "message": "Virtual website inventory"
    })


@app.route("/api/catalog")
@login_required
def api_catalog():
    try:
        items = virtual_catalog()
        return jsonify({"ok": True, "items": items, "source": "BIG_GAMES_PUBLIC_CATALOG"})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not load PS99 catalog: {exc}"}), 502


@app.route("/api/inventory/deposit", methods=["POST"])
@login_required
def api_virtual_deposit():
    user = current_user()
    data = request.get_json(silent=True) or {}
    item_id = str(data.get("item_id", "")).strip()
    try:
        quantity = int(data.get("quantity", 1))
    except (TypeError, ValueError):
        quantity = 1
    quantity = max(1, min(quantity, 1000))
    if not item_id:
        return jsonify({"ok": False, "error": "Choose a pet."}), 400

    catalog = virtual_catalog()
    selected = next((x for x in catalog if x["item_id"] == item_id), None)
    if not selected:
        return jsonify({"ok": False, "error": "Pet not found in the current PS99 catalog."}), 404
    if float(selected["value"]) <= 0:
        return jsonify({"ok": False, "error": "This pet does not currently have a usable RAP value."}), 400

    conn = db()
    source_key = f"virtual:Pet:{selected['item_id']}"
    existing = conn.execute(
        "SELECT * FROM inventory_items WHERE owner_id=? AND source_key=?",
        (user["id"], source_key)
    ).fetchone()
    now = int(time.time())
    if existing:
        conn.execute(
            "UPDATE inventory_items SET name=?, item_class=?, item_id=?, variant=?, icon_url=?, value=?, count=count+?, updated_at=? WHERE id=?",
            (selected["name"], "Pet", selected["item_id"], selected.get("variant", ""), selected.get("icon", ""), float(selected["value"]), quantity, now, existing["id"])
        )
    else:
        conn.execute(
            """INSERT INTO inventory_items
               (owner_id,source_key,item_class,item_id,name,variant,icon_url,value,count,source_count,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user["id"], source_key, "Pet", selected["item_id"], selected["name"], selected.get("variant", ""), selected.get("icon", ""), float(selected["value"]), quantity, 0, now, now)
        )
    conn.commit(); conn.close()
    return jsonify({"ok": True, "message": f"Added {quantity}× {selected['name']} to your virtual inventory.", "item": selected})


@app.route("/api/inventory")
@login_required
def api_inventory():
    user = current_user()
    conn = db()
    rows = conn.execute("SELECT * FROM inventory_items WHERE owner_id=? AND count>0 ORDER BY value DESC, name COLLATE NOCASE", (user["id"],)).fetchall()
    conn.close()
    return jsonify({
        "ok": True,
        "virtual_inventory": True,
        "items": [compact_item_snapshot(r) | {"count": int(r["count"]), "total_value": float(r["value"] or 0) * int(r["count"])} for r in rows],
        "total_value": sum(float(r["value"] or 0) * int(r["count"]) for r in rows),
        "item_count": sum(int(r["count"] or 0) for r in rows)
    })


@app.route("/api/give-item", methods=["POST"])
@login_required
def api_give_item():
    sender = current_user()
    data = request.get_json(silent=True) or {}
    try:
        recipient_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid recipient."}), 400
    code = str(data.get("code", "")).strip()
    if not GIVE_ITEM_CODE:
        return jsonify({"ok": False, "error": "Give Item permission code is not configured on the server."}), 503
    if not secrets.compare_digest(code, GIVE_ITEM_CODE):
        return jsonify({"ok": False, "error": "Invalid permission code."}), 403
    if recipient_id == sender["id"]:
        return jsonify({"ok": False, "error": "You cannot give items to yourself."}), 400
    item_ids = data.get("item_ids", [])
    if not isinstance(item_ids, list): item_ids = [item_ids]
    clean_ids = []
    for x in item_ids:
        try: clean_ids.append(int(x))
        except (TypeError, ValueError): pass
    clean_ids = list(dict.fromkeys(clean_ids))
    if not clean_ids:
        return jsonify({"ok": False, "error": "Select at least one item."}), 400
    conn = db()
    recipient = conn.execute("SELECT id, display_name FROM users WHERE id=?", (recipient_id,)).fetchone()
    if not recipient:
        conn.close(); return jsonify({"ok": False, "error": "User not found."}), 404
    placeholders = ",".join("?" for _ in clean_ids)
    rows = conn.execute(f"SELECT * FROM inventory_items WHERE owner_id=? AND id IN ({placeholders}) AND count>0", [sender["id"], *clean_ids]).fetchall()
    if len(rows) != len(clean_ids):
        conn.close(); return jsonify({"ok": False, "error": "One or more selected items are no longer available."}), 400
    snapshots = [compact_item_snapshot(r) for r in rows]
    now = int(time.time())
    for row in rows:
        conn.execute("UPDATE inventory_items SET count=0, updated_at=? WHERE id=?", (now, row["id"]))
    grant_item_snapshots(conn, recipient_id, snapshots)
    sender_name = sender["display_name"]
    public_message = f"🎁 {sender_name} gave {recipient['display_name']} {len(snapshots)} item(s)!"
    conn.execute("INSERT INTO chat(user_id,message,created_at) VALUES(?,?,?)", (sender["id"], public_message, now))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "message": public_message, "items": snapshots})


# ============================================================
# API - MATCH LIST
# ============================================================

@app.route("/api/matches")
def api_matches():

    conn = db()

    now = int(time.time())

    rows = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE (
            status IN ('waiting', 'ready')
            AND created_at >= ?
        )
        OR (
            status = 'finished'
            AND COALESCE(finished_at, created_at) >= ?
        )
        ORDER BY
            CASE status
                WHEN 'waiting' THEN 0
                WHEN 'ready' THEN 1
                ELSE 2
            END,
            id DESC
        LIMIT 100
        """,
        (
            now - 180,
            now - 180
        )
    ).fetchall()

    conn.close()

    return jsonify([
        match_json(row)
        for row in rows
    ])


@app.route("/api/live-wins")
def api_live_wins():
    """Return recent completed matches with a real user as the winner."""
    conn = db()
    rows = conn.execute(
        """
        SELECT m.id AS match_id, m.winner_id, m.bet, m.match_mode,
               m.best_of, m.finished_at,
               u.username, u.display_name, u.avatar
        FROM matches AS m
        JOIN users AS u ON u.id = m.winner_id
        WHERE m.status = 'finished'
          AND m.winner_id IS NOT NULL
          AND m.winner_id > 0
          AND COALESCE(m.finished_at, m.created_at) >= ?
        ORDER BY COALESCE(m.finished_at, m.created_at) DESC, m.id DESC
        LIMIT 12
        """,
        (int(time.time()) - 86400,)
    ).fetchall()
    conn.close()

    return jsonify([
        {
            "match_id": row["match_id"],
            "winner_id": row["winner_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "avatar": row["avatar"],
            "bet": float(row["bet"] or 0),
            "payout": float(row["bet"] or 0) * (3 if row["match_mode"] == "1v1v1" else 2),
            "match_mode": row["match_mode"] or "1v1",
            "best_of": int(row["best_of"] or 1),
            "finished_at": row["finished_at"]
        }
        for row in rows
    ])


# ============================================================
# CREATE MATCH
# ============================================================

@app.route(
    "/api/matches/create",
    methods=["POST"]
)
@login_required
def create_match():
    user = current_user()
    data = request.get_json(silent=True) or {}
    raw_mode = str(data.get("mode", "1v1"))
    match_mode = "1v1v1" if raw_mode == "1v1v1" else "1v1"
    try:
        best_of = int(data.get("best_of", 1))
    except (TypeError, ValueError):
        best_of = 1
    if best_of not in (1, 2, 3, 5): best_of = 1

    colors = data.get("colors", [])
    if not isinstance(colors, list): colors = [colors]
    clean_colors = list(dict.fromkeys(str(c).lower() for c in colors if str(c).lower() in COLORS))
    if not 1 <= len(clean_colors) <= 2:
        return jsonify({"ok": False, "error": "Choose 1 or 2 colors."}), 400

    item_ids = data.get("item_ids", [])
    if not isinstance(item_ids, list): item_ids = [item_ids]
    clean_ids = []
    for x in item_ids:
        try: clean_ids.append(int(x))
        except (TypeError, ValueError): pass
    clean_ids = list(dict.fromkeys(clean_ids))
    if not clean_ids:
        return jsonify({"ok": False, "error": "Choose at least one item for your wager."}), 400

    conn = db()
    active = conn.execute("SELECT id FROM matches WHERE creator_id=? AND status IN ('waiting','ready')", (user["id"],)).fetchone()
    if active:
        conn.close(); return jsonify({"ok": False, "error": "You already have an active match."}), 400
    placeholders = ",".join("?" for _ in clean_ids)
    rows = conn.execute(f"SELECT * FROM inventory_items WHERE owner_id=? AND id IN ({placeholders}) AND count>0", [user["id"], *clean_ids]).fetchall()
    if len(rows) != len(clean_ids):
        conn.close(); return jsonify({"ok": False, "error": "One or more selected items are unavailable."}), 400
    snapshots = [compact_item_snapshot(r) for r in rows]
    bet = item_snapshots_value(snapshots)
    if bet <= 0:
        conn.close(); return jsonify({"ok": False, "error": "Selected items do not have a valid Big Games value."}), 400
    if bet > MAX_BET:
        conn.close(); return jsonify({"ok": False, "error": "Maximum wager is 500B."}), 400
    now = int(time.time())
    for row in rows:
        conn.execute("UPDATE inventory_items SET count=0, updated_at=? WHERE id=?", (now, row["id"]))
    cursor = conn.execute(
        """INSERT INTO matches
        (creator_id,bet,creator_color,status,created_at,opponent_is_bot,match_mode,best_of,creator_items,opponent_items,third_items)
        VALUES (?,?,?,?,?,0,?,?,?, '[]','[]')""",
        (user["id"], bet, save_colors(clean_colors), "waiting", now, match_mode, best_of, json.dumps(snapshots))
    )
    match_id = cursor.lastrowid
    conn.commit(); conn.close()
    return jsonify({"ok": True, "match_id": match_id, "value": bet})


# ============================================================
# CANCEL MATCH
# ============================================================

@app.route(
    "/api/matches/<int:match_id>/cancel",
    methods=["POST"]
)
@login_required
def cancel_match(match_id):
    user = current_user(); conn = db()
    row = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"ok": False, "error": "Match not found."}), 404
    if row["creator_id"] != user["id"]:
        conn.close(); return jsonify({"ok": False, "error": "Only the creator can cancel this match."}), 403
    if row["status"] != "waiting":
        conn.close(); return jsonify({"ok": False, "error": "This match can no longer be cancelled."}), 400
    grant_item_snapshots(conn, row["creator_id"], parse_item_snapshots(row["creator_items"]))
    if row["opponent_id"] and not row["opponent_is_bot"]:
        grant_item_snapshots(conn, row["opponent_id"], parse_item_snapshots(row["opponent_items"]))
    if row["third_player_id"] and not row["third_is_bot"]:
        grant_item_snapshots(conn, row["third_player_id"], parse_item_snapshots(row["third_items"]))
    conn.execute("UPDATE matches SET status='cancelled', finished_at=? WHERE id=?", (int(time.time()), match_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "message": "Match cancelled and items refunded."})


# ============================================================
# JOIN PLAYER
# ============================================================

@app.route(
    "/api/matches/<int:match_id>/join",
    methods=["POST"]
)
@login_required
def join_match(match_id):
    user = current_user(); conn = db()
    row = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"ok": False, "error": "Match not found."}), 404
    is_three = row["match_mode"] == "1v1v1"
    waiting_for_third = is_three and row["opponent_id"] and not row["third_player_id"]
    if row["status"] != "waiting" or (is_three and row["opponent_id"] and row["third_player_id"]):
        conn.close(); return jsonify({"ok": False, "error": "This match is no longer waiting."}), 400
    if user["id"] in [row["creator_id"], row["opponent_id"], row["third_player_id"]]:
        conn.close(); return jsonify({"ok": False, "error": "You are already in this match."}), 400
    active = conn.execute("SELECT id FROM matches WHERE (opponent_id=? OR third_player_id=?) AND status IN ('waiting','ready')", (user["id"], user["id"])).fetchone()
    if active:
        conn.close(); return jsonify({"ok": False, "error": "You already have an active match."}), 400

    creator_colors = parse_colors(row["creator_color"])
    data = request.get_json(silent=True) or {}
    chosen = data.get("colors", [])
    if not isinstance(chosen, list): chosen = [chosen]
    chosen = list(dict.fromkeys(str(c).lower() for c in chosen if str(c).lower() in COLORS))
    if len(chosen) != len(creator_colors):
        conn.close(); return jsonify({"ok": False, "error": f"Choose exactly {len(creator_colors)} color(s)."}), 400
    taken = set(creator_colors) | set(parse_colors(row["opponent_color"])) | set(parse_colors(row["third_color"]))
    if taken.intersection(chosen):
        conn.close(); return jsonify({"ok": False, "error": "Those colors are already taken."}), 400

    item_ids = data.get("item_ids", [])
    if not isinstance(item_ids, list): item_ids = [item_ids]
    clean_ids=[]
    for x in item_ids:
        try: clean_ids.append(int(x))
        except (TypeError, ValueError): pass
    clean_ids=list(dict.fromkeys(clean_ids))
    if not clean_ids:
        conn.close(); return jsonify({"ok": False, "error": "Choose at least one item for your wager."}), 400
    placeholders=",".join("?" for _ in clean_ids)
    rows=conn.execute(f"SELECT * FROM inventory_items WHERE owner_id=? AND id IN ({placeholders}) AND count>0", [user["id"], *clean_ids]).fetchall()
    if len(rows)!=len(clean_ids):
        conn.close(); return jsonify({"ok": False, "error": "One or more selected items are unavailable."}), 400
    snapshots=[compact_item_snapshot(r) for r in rows]
    bet=item_snapshots_value(snapshots)
    if bet<=0 or bet>MAX_BET:
        conn.close(); return jsonify({"ok": False, "error": "Selected item value is invalid or exceeds 500B."}), 400
    now=int(time.time())
    for r in rows: conn.execute("UPDATE inventory_items SET count=0, updated_at=? WHERE id=?", (now,r["id"]))
    if waiting_for_third:
        conn.execute("UPDATE matches SET third_player_id=?, third_color=?, third_items=?, status='ready' WHERE id=?", (user["id"], save_colors(chosen), json.dumps(snapshots), match_id))
    else:
        conn.execute("UPDATE matches SET opponent_id=?, opponent_color=?, opponent_items=?, opponent_is_bot=0, status=? WHERE id=?", (user["id"], save_colors(chosen), json.dumps(snapshots), "ready" if not is_three else "waiting", match_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "match_id": match_id, "status": "ready" if (waiting_for_third or not is_three) else "waiting"})


@app.route(
    "/api/matches/<int:match_id>/call-bot",
    methods=["POST"]
)
@login_required
def call_bot(match_id):
    user=current_user(); conn=db()
    row=conn.execute("SELECT * FROM matches WHERE id=?",(match_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"ok":False,"error":"Match not found."}),404
    if row["creator_id"]!=user["id"]:
        conn.close(); return jsonify({"ok":False,"error":"Only the creator can call the bot."}),403
    if row["status"]!="waiting":
        conn.close(); return jsonify({"ok":False,"error":"This match is no longer waiting."}),400
    creator_colors=parse_colors(row["creator_color"])
    available=[c for c in COLOR_NAMES if c not in creator_colors]
    bot_colors=random.sample(available,min(len(creator_colors),len(available)))
    if row["match_mode"]=="1v1v1":
        remaining=[c for c in available if c not in bot_colors]
        bot2_colors=random.sample(remaining,min(len(creator_colors),len(remaining)))
        bot_item=[{"virtual":True,"name":"ColorDice Bot Pool","item_id":"BOT_POOL","item_class":"Virtual","icon":"","value":float(row["bet"]),"count":1,"variant":""}]
        bot2_item=[{"virtual":True,"name":"ColorDice Bot Pool","item_id":"BOT_POOL_2","item_class":"Virtual","icon":"","value":float(row["bet"]),"count":1,"variant":""}]
        conn.execute("UPDATE matches SET opponent_id=NULL, opponent_color=?, opponent_items=?, opponent_is_bot=1, third_player_id=NULL, third_color=?, third_items=?, third_is_bot=1, status='ready' WHERE id=?", (save_colors(bot_colors),json.dumps(bot_item),save_colors(bot2_colors),json.dumps(bot2_item),match_id))
    else:
        bot_item=[{"virtual":True,"name":"ColorDice Bot Pool","item_id":"BOT_POOL","item_class":"Virtual","icon":"","value":float(row["bet"]),"count":1,"variant":""}]
        conn.execute("UPDATE matches SET opponent_id=NULL, opponent_color=?, opponent_items=?, opponent_is_bot=1, status='ready' WHERE id=?", (save_colors(bot_colors),json.dumps(bot_item),match_id))
    conn.commit(); conn.close()
    return jsonify({"ok":True,"match_id":match_id,"bot_colors":bot_colors,"status":"ready"})


@app.route("/api/matches/<int:match_id>/start", methods=["POST"])
@login_required
def start_match(match_id):
    user=current_user(); conn=db(); row=conn.execute("SELECT * FROM matches WHERE id=?",(match_id,)).fetchone(); conn.close()
    if not row: return jsonify({"ok":False,"error":"Match not found."}),404
    if row["creator_id"]!=user["id"]: return jsonify({"ok":False,"error":"Only the creator can start this match."}),403
    if row["status"]!="ready": return jsonify({"ok":False,"error":"The match is not ready yet."}),400
    result=settle_match(match_id)
    if not result: return jsonify({"ok":False,"error":"Could not start the match."}),500
    return jsonify({"ok":True,"status":"finished","match":match_json(result)})


# ============================================================
# SETTLE MATCH
# ============================================================

def settle_item_match(match_id):
    """Resolve a PS99 item-backed match and transfer the website-side wager items."""
    conn=db(); row=conn.execute("SELECT * FROM matches WHERE id=?",(match_id,)).fetchone()
    if not row or row["status"]!="ready": conn.close(); return None
    creator=row["creator_id"]; opponent=row["opponent_id"] if row["opponent_id"] else (-1 if row["opponent_is_bot"] else None)
    third_id=row["third_player_id"] if row["third_player_id"] else (-2 if row["third_is_bot"] else None)
    if opponent is None or (row["match_mode"]=="1v1v1" and third_id is None): conn.close(); return None
    players=[{"id":creator,"colors":parse_colors(row["creator_color"]),"bot":False},{"id":opponent,"colors":parse_colors(row["opponent_color"]),"bot":bool(row["opponent_is_bot"])}]
    if row["match_mode"]=="1v1v1": players.append({"id":third_id,"colors":parse_colors(row["third_color"]),"bot":bool(row["third_is_bot"])})
    history=[]; winner=None; best_of=int(row["best_of"] or 1)

    if row["match_mode"]=="1v1v1" and best_of==1:
        alive=list(range(3))
        for _ in range(100):
            rolls=roll_round(); history.append(rolls)
            scores=[color_score(rolls,p["colors"]) for p in players]
            high=max(scores); leaders=[i for i in alive if scores[i]==high]
            if len(leaders)==1:
                winner=players[leaders[0]]["id"]; break
            alive=leaders
        if winner is None: winner=players[alive[0]]["id"]
    elif best_of>1 and row["match_mode"]=="1v1":
        target=best_of; score_map={str(creator):0,str(opponent):0}
        for _ in range(target*2-1):
            point=None; last=(0,0)
            for _ in range(100):
                rolls=roll_round(); history.append(rolls)
                last=(color_score(rolls,players[0]["colors"]),color_score(rolls,players[1]["colors"]))
                if last[0]!=last[1]: point=creator if last[0]>last[1] else opponent; break
            if point is None: point=creator if last[0]>last[1] else opponent
            score_map[str(point)]+=1
            if score_map[str(point)]>=target: winner=point; break
        if winner is None: winner=creator
    else:
        for _ in range(100):
            rolls=roll_round(); history.append(rolls)
            scores=[color_score(rolls,p["colors"]) for p in players]
            if scores[0]!=scores[1]: winner=players[0]["id"] if scores[0]>scores[1] else players[1]["id"]; break
        if winner is None: winner=players[0]["id"]

    pot=[]
    for field in ("creator_items","opponent_items","third_items"):
        pot.extend(parse_item_snapshots(row[field]) if field in row.keys() else [])
    human_wagers=[item for item in pot if not item.get("virtual")]
    if winner>0:
        grant_item_snapshots(conn,winner,human_wagers)
        conn.execute("UPDATE users SET wins=wins+1,games=games+1 WHERE id=?",(winner,))
    for p in players:
        if p["id"]>0 and p["id"]!=winner:
            conn.execute("UPDATE users SET losses=losses+1,games=games+1 WHERE id=?",(p["id"],))
    if row["match_mode"]=="1v1" and row["opponent_is_bot"] and winner==creator:
        conn.execute("UPDATE users SET balance=balance+? WHERE id=?",(float(row["bet"]),creator))
    started=int(time.time()*1000)
    conn.execute("UPDATE matches SET status='finished',winner_id=?,rolls=?,finished_at=?,animation_started_at=? WHERE id=?",(winner,json.dumps(history),int(time.time()),started,match_id))
    conn.commit(); final=conn.execute("SELECT * FROM matches WHERE id=?",(match_id,)).fetchone(); conn.close(); return final

def settle_match(match_id):

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE id = ?
        """,
        (match_id,)
    ).fetchone()

    if not row:
        conn.close()
        return None

    if row["status"] != "ready":
        conn.close()
        return None

    # Item-backed matches use the same dice rules but settle by transferring
    # the wagered item records rather than paying the old dollar wager.
    if "creator_items" in row.keys() and parse_item_snapshots(row["creator_items"]):
        conn.close()
        return settle_item_match(match_id)

    creator = row["creator_id"]
    opponent = row["opponent_id"]

    if not opponent and not row["opponent_is_bot"]:
        conn.close()
        return None

    creator_colors = parse_colors(
        row["creator_color"]
    )

    opponent_colors = parse_colors(
        row["opponent_color"]
    )

    if row["match_mode"] == "1v1v1":
        players = [
            {"id": creator, "colors": parse_colors(row["creator_color"]), "bot": False},
            {"id": opponent if opponent else -1, "colors": parse_colors(row["opponent_color"]), "bot": bool(row["opponent_is_bot"])},
            {"id": row["third_player_id"] if row["third_player_id"] else -2, "colors": parse_colors(row["third_color"]), "bot": bool(row["third_is_bot"])},
        ]
        if not row["opponent_id"] and not row["opponent_is_bot"]:
            conn.close()
            return None
        if not row["third_player_id"] and not row["third_is_bot"]:
            conn.close()
            return None
        alive = list(range(3))
        roll_history = []
        winner = None
        for _ in range(100):
            rolls = roll_round()
            roll_history.append(rolls)
            scores = {i: color_score(rolls, players[i]["colors"]) for i in alive}
            high = max(scores.values())
            survivors = [i for i in alive if scores[i] == high]
            # A unique highest scorer wins; otherwise only the tied leaders reroll.
            if len(survivors) == 1:
                winner = players[survivors[0]]["id"]
                break
            alive = survivors
        if winner is None:
            winner = players[alive[0]]["id"]
        bet = float(row["bet"])
        winner_is_human = winner > 0
        if winner_is_human:
            conn.execute("UPDATE users SET balance = balance + ?, wins = wins + 1, games = games + 1 WHERE id = ?", (bet * 3, winner))
        for p in players:
            if p["id"] > 0 and p["id"] != winner:
                conn.execute("UPDATE users SET losses = losses + 1, games = games + 1 WHERE id = ?", (p["id"],))
        animation_started_at = int(time.time() * 1000)
        conn.execute(
            "UPDATE matches SET status='finished', winner_id=?, rolls=?, finished_at=?, animation_started_at=? WHERE id=?",
            (winner, json.dumps(roll_history), int(time.time()), animation_started_at, match_id)
        )
        conn.commit()
        final = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        conn.close()
        return final

    # --------------------------------------------------------
    # Keep rolling until the scores are different.
    # Every round is saved so the browser can replay it as:
    # 3...2...1 -> rolling -> reveal one by one -> history.
    # --------------------------------------------------------

    roll_history = []
    final_creator_score = 0
    final_opponent_score = 0
    winner = None
    best_of = int(row["best_of"] or 1) if "best_of" in row.keys() else 1
    if best_of > 1:
        target_wins = best_of
        series_wins = {str(creator): 0, str(opponent if opponent else -1): 0}
        series_results = []
        # Each match point is a full dice duel; ties reroll within that point.
        for _game in range(target_wins * 2 - 1):
            point_winner = None
            last_scores = (0, 0)
            for _roll in range(100):
                rolls = roll_round()
                roll_history.append(rolls)
                cs = color_score(rolls, creator_colors)
                oscore = color_score(rolls, opponent_colors)
                last_scores = (cs, oscore)
                if cs > oscore:
                    point_winner = creator
                    break
                if oscore > cs:
                    point_winner = opponent if opponent else -1
                    break
            if point_winner is None:
                point_winner = creator if last_scores[0] > last_scores[1] else (opponent if opponent else -1)
            series_wins[str(point_winner)] = series_wins.get(str(point_winner), 0) + 1
            series_results.append({"winner_id": point_winner, "scores": {str(creator): last_scores[0], str(opponent if opponent else -1): last_scores[1]}, "roll_index": len(roll_history) - 1})
            if series_wins[str(point_winner)] >= target_wins:
                winner = point_winner
                break
    else:
        for _ in range(100):
            rolls = roll_round()
            roll_history.append(rolls)
            creator_score = color_score(rolls, creator_colors)
            opponent_score = color_score(rolls, opponent_colors)
            final_creator_score = creator_score
            final_opponent_score = opponent_score
            if creator_score > opponent_score:
                winner = creator
                break
            if opponent_score > creator_score:
                winner = opponent if opponent else -1
                break
        if winner is None:
            winner = creator if final_creator_score > final_opponent_score else (opponent if opponent else -1)

    if best_of > 1:
        conn.execute("UPDATE matches SET series_wins=? WHERE id=?", (json.dumps({"target": best_of, "scores": series_wins, "results": series_results}), match_id))

    bet = float(row["bet"])

    # --------------------------------------------------------
    # Human vs Human
    # --------------------------------------------------------

    if not row["opponent_is_bot"]:

        pot = bet * 2

        conn.execute(
            """
            UPDATE users
            SET balance = balance + ?
            WHERE id = ?
            """,
            (
                pot,
                winner
            )
        )

        loser = (
            opponent
            if winner == creator
            else creator
        )

        conn.execute(
            """
            UPDATE users
            SET
                wins = wins + 1,
                games = games + 1
            WHERE id = ?
            """,
            (winner,)
        )

        conn.execute(
            """
            UPDATE users
            SET
                losses = losses + 1,
                games = games + 1
            WHERE id = ?
            """,
            (loser,)
        )

    # --------------------------------------------------------
    # Human vs Bot
    # --------------------------------------------------------

    else:

        if winner == creator:

            payout = bet * 2

            conn.execute(
                """
                UPDATE users
                SET balance = balance + ?
                WHERE id = ?
                """,
                (
                    payout,
                    creator
                )
            )

            conn.execute(
                """
                UPDATE users
                SET
                    wins = wins + 1,
                    games = games + 1
                WHERE id = ?
                """,
                (creator,)
            )

        else:

            conn.execute(
                """
                UPDATE users
                SET
                    losses = losses + 1,
                    games = games + 1
                WHERE id = ?
                """,
                (creator,)
            )

    animation_started_at = int(time.time() * 1000)

    conn.execute(
        """
        UPDATE matches
        SET
            status = 'finished',
            winner_id = ?,
            rolls = ?,
            finished_at = ?,
            animation_started_at = ?
        WHERE id = ?
        """,
        (
            winner,
            json.dumps(roll_history),
            int(time.time()),
            animation_started_at,
            match_id
        )
    )

    conn.commit()

    final = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE id = ?
        """,
        (match_id,)
    ).fetchone()

    conn.close()

    return final


# ============================================================
# SINGLE MATCH
# ============================================================

@app.route(
    "/api/matches/<int:match_id>"
)
def api_match(match_id):

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE id = ?
        """,
        (match_id,)
    ).fetchone()

    conn.close()

    if not row:

        return jsonify({
            "ok": False,
            "error": "Not found."
        }), 404

    return jsonify({
        "ok": True,
        "match": match_json(row)
    })


# ============================================================
# LEADERBOARD
# ============================================================

@app.route("/api/leaderboard")
def api_leaderboard():

    conn = db()

    rows = conn.execute(
        """
        SELECT
            id,
            username,
            display_name,
            avatar,
            balance,
            wins,
            losses,
            games
        FROM users
        ORDER BY balance DESC
        LIMIT 20
        """
    ).fetchall()

    conn.close()

    return jsonify([
        dict(row)
        for row in rows
    ])


# ============================================================
# USER PROFILE
# ============================================================

@app.route("/api/users/<int:user_id>")
def api_user_profile(user_id):

    conn = db()

    user = conn.execute(
        """
        SELECT
            id,
            username,
            display_name,
            avatar,
            balance,
            wins,
            losses,
            games,
            total_wager
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    ).fetchone()

    if not user:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "User not found."
        }), 404

    matches = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE creator_id = ?
           OR opponent_id = ?
           OR third_player_id = ?
        ORDER BY id DESC
        LIMIT 25
        """,
        (user_id, user_id, user_id)
    ).fetchall()

    conn.close()

    history = []

    for row in matches:
        if row["status"] != "finished":
            continue

        if row["creator_id"] == user_id:
            opponent = (
                bot_public_user()
                if row["opponent_is_bot"]
                else public_user(row["opponent_id"])
            )
            won = row["winner_id"] == user_id
        else:
            opponent = public_user(row["creator_id"])
            won = row["winner_id"] == user_id

        history.append({
            "id": row["id"],
            "bet": float(row["bet"]),
            "result": "WIN" if won else "LOSS",
            "opponent": opponent,
            "finished_at": row["finished_at"],
        })

    return jsonify({
        "ok": True,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "avatar": user["avatar"] or
                      "https://cdn.discordapp.com/embed/avatars/0.png",
            "balance": float(user["balance"]),
            "wins": user["wins"],
            "losses": user["losses"],
            "games": user["games"],
            "total_wager": float(user["total_wager"] or 0),
            "level": user_level(user["total_wager"] or 0),
        },
        "history": history,
    })


# ============================================================
# TIP TRANSFER
# ============================================================
@app.route("/api/tip", methods=["POST"])
@login_required
def api_tip():
    sender = current_user()
    data = request.get_json(silent=True) or {}
    try:
        recipient_id = int(data.get("user_id"))
        amount = round(float(data.get("amount", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid recipient or amount."}), 400
    if recipient_id == sender["id"]:
        return jsonify({"ok": False, "error": "You cannot tip yourself."}), 400
    if amount < 0.01 or amount > 1000000:
        return jsonify({"ok": False, "error": "Tip must be between 0.01 and 1,000,000 Gems."}), 400
    conn = db()
    recipient = conn.execute("SELECT id, display_name FROM users WHERE id=?", (recipient_id,)).fetchone()
    balance = conn.execute("SELECT balance FROM users WHERE id=?", (sender["id"],)).fetchone()
    if not recipient:
        conn.close()
        return jsonify({"ok": False, "error": "User not found."}), 404
    if not balance or float(balance["balance"]) < amount:
        conn.close()
        return jsonify({"ok": False, "error": "Insufficient balance."}), 400
    conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (amount, sender["id"]))
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, recipient_id))
    sender_name = conn.execute("SELECT display_name FROM users WHERE id=?", (sender["id"],)).fetchone()["display_name"]
    public_message = f"💸 {sender_name} tipped {recipient['display_name']} {money(amount)}!"
    conn.execute("INSERT INTO chat(user_id,message,created_at) VALUES(?,?,?)", (sender["id"], public_message, int(time.time())))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": public_message, "balance": float(balance["balance"])-amount})


# ============================================================
# CHAT
# ============================================================

@app.route(
    "/api/chat",
    methods=["GET", "POST"]
)
def api_chat():

    user = current_user()

    if request.method == "POST":

        if not user:

            return jsonify({
                "ok": False,
                "error": "Login required."
            }), 401

        data = (
            request.get_json(
                silent=True
            ) or {}
        )

        message = str(
            data.get(
                "message",
                ""
            )
        ).strip()

        if not message:

            return jsonify({
                "ok": False,
                "error": "Empty message."
            }), 400

        if len(message) > 250:

            return jsonify({
                "ok": False,
                "error":
                    "Message too long."
            }), 400

        conn = db()

        conn.execute(
            """
            INSERT INTO chat
            (
                user_id,
                message,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                user["id"],
                message,
                int(time.time())
            )
        )

        conn.execute(
            """
            DELETE FROM chat
            WHERE id NOT IN
            (
                SELECT id
                FROM chat
                ORDER BY id DESC
                LIMIT 150
            )
            """
        )

        conn.commit()
        conn.close()

        return jsonify({
            "ok": True
        })

    conn = db()

    rows = conn.execute(
        """
        SELECT
            chat.id,
            chat.user_id,
            chat.message,
            chat.created_at,
            users.username,
            users.display_name,
            users.avatar,
            users.total_wager
        FROM chat
        JOIN users
        ON users.id = chat.user_id
        ORDER BY chat.id DESC
        LIMIT 50
        """
    ).fetchall()

    conn.close()

    return jsonify([
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "message": row["message"],
            "created_at":
                row["created_at"],
            "username":
                row["username"],
            "display_name":
                row["display_name"],
            "avatar":
                row["avatar"],
            "level":
                user_level(row["total_wager"] or 0),
        }
        for row in reversed(rows)
    ])


# ============================================================
# FRONTEND
# ============================================================

PAGE = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>ColorDice</title>

<style>
* {
    font-family:
        Inter,
        Pretendard,
        "Noto Sans KR",
        "Segoe UI",
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        sans-serif;
    letter-spacing: -0.01em;
}

body {
    font-feature-settings: "cv02", "cv03", "cv04", "cv11";
}

* {
    box-sizing: border-box;
}

:root {

    --bg: #080d17;
    --panel: #101827;
    --panel2: #131d2e;
    --line: #243149;

    --text: #f4f7fb;
    --muted: #8d9bb2;

    --blue: #3b82f6;
    --blue2: #2563eb;

    --green: #19b66a;
    --red: #ef3340;
    --purple: #963cf2;
    --orange: #f97316;
    --yellow: #f5c542;
}

html,
body {

    margin: 0;

    min-height: 100%;

    background:
        radial-gradient(
            circle at 20% -10%,
            rgba(43,105,190,.18),
            transparent 30%
        ),
        radial-gradient(
            circle at 90% 10%,
            rgba(74,44,160,.12),
            transparent 28%
        ),
        var(--bg);

    color: var(--text);

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

button,
input,
select {
    font: inherit;
}

button {
    cursor: pointer;
}

.topbar {

    height: 64px;

    position: fixed;

    left: 0;
    right: 0;
    top: 0;

    z-index: 50;

    display: flex;
    align-items: center;

    padding: 0 16px;

    background:
        rgba(9,15,25,.96);

    border-bottom:
        1px solid var(--line);

    backdrop-filter:
        blur(18px);
}

.brand {

    width: 220px;

    display: flex;
    align-items: center;

    gap: 10px;

    font-weight: 900;

    font-size: 21px;
}

.brand-mark {

    width: 36px;
    height: 36px;

    border-radius: 11px;

    display: grid;
    place-items: center;

    background:
        linear-gradient(
            135deg,
            #2f7df4,
            #7c3aed
        );
}

.top-actions {

    margin-left: auto;

    display: flex;

    align-items: center;

    gap: 9px;
}

.balance-box {

    height: 42px;

    min-width: 135px;

    padding: 0 13px;

    display: flex;
    align-items: center;

    gap: 8px;

    border:
        1px solid var(--line);

    border-radius: 10px;

    background: #101b2c;
}

.dollar {

    color: #65d7ff;

    font-weight: 900;
}

.wallet-btn {

    height: 42px;

    padding: 0 17px;

    border: 0;

    border-radius: 10px;

    color: white;

    background:
        linear-gradient(
            135deg,
            var(--blue),
            #4d79ff
        );

    font-weight: 900;
}

.user-chip {

    height: 42px;

    padding: 4px 10px 4px 5px;

    display: flex;

    align-items: center;

    gap: 8px;

    border:
        1px solid var(--line);

    border-radius: 10px;

    background: #111c2c;
}

.user-chip img {

    width: 32px;
    height: 32px;

    border-radius: 9px;

    object-fit: cover;
}

.login-btn {

    padding: 12px 18px;

    border: 0;

    border-radius: 10px;

    background: #5865f2;

    color: white;

    font-weight: 900;
}

.layout {

    display: grid;

    grid-template-columns:
        68px
        minmax(0,1fr)
        310px;

    min-height: 100vh;

    padding-top: 64px;
}

.sidebar {

    position: fixed;

    left: 0;
    top: 64px;
    bottom: 0;

    width: 68px;

    background: #0b1320;

    border-right:
        1px solid var(--line);

    display: flex;

    flex-direction: column;

    align-items: center;

    padding-top: 14px;
}

.nav-btn {

    width: 44px;
    height: 44px;

    margin: 4px 0;

    border: 0;

    border-radius: 10px;

    background: transparent;

    color: #8090a8;

    font-size: 19px;
}

.nav-btn:hover,
.nav-btn.active {

    color: white;

    background: #17263c;

    box-shadow:
        inset 3px 0 0 var(--blue);
}

.main {

    grid-column: 2;

    min-width: 0;

    padding:
        18px
        18px
        40px;
}

.chat {

    position: fixed;

    right: 0;
    top: 64px;
    bottom: 0;

    width: 310px;

    display: flex;

    flex-direction: column;

    background: #0c1523;

    border-left:
        1px solid var(--line);
}

.chat-head {

    height: 54px;

    padding: 0 16px;

    display: flex;

    align-items: center;

    justify-content: space-between;

    border-bottom:
        1px solid var(--line);

    font-weight: 900;
}

.online {

    color: #23d18b;

    font-size: 11px;
}

.chat-list {

    flex: 1;

    overflow-y: auto;

    padding: 10px;
}

.chat-msg {
    position: relative;
    display: flex;
    gap: 10px;
    padding: 8px 9px;
    margin-bottom: 4px;
    border-radius: 10px;
    cursor: pointer;
    transition: background .16s, transform .16s;
}

.chat-msg:hover {
    background: #111f31;
    transform: translateX(1px);
}

.chat-avatar-wrap {
    position: relative;
    flex: 0 0 34px;
    width: 34px;
    height: 34px;
}

.chat-msg img {
    width: 34px;
    height: 34px;
    border-radius: 9px;
    object-fit: cover;
}

.chat-level {
    position: absolute;
    left: -2px;
    bottom: -4px;
    min-width: 21px;
    height: 15px;
    padding: 0 4px;
    display: grid;
    place-items: center;
    border-radius: 5px;
    background: #14b8d4;
    color: #07141b;
    border: 1px solid #081a25;
    font-size: 8px;
    font-weight: 1000;
    line-height: 1;
    box-shadow: 0 2px 8px rgba(0,0,0,.3);
}

.chat-body {
    min-width: 0;
    flex: 1;
}

.chat-meta {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
}

.chat-name {
    color: #55d8ef;
    font-size: 12px;
    font-weight: 950;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.chat-verified {
    width: 13px;
    height: 13px;
    display: grid;
    place-items: center;
    border-radius: 50%;
    background: #19b8dc;
    color: white;
    font-size: 8px;
    font-weight: 1000;
}

.chat-time {
    margin-left: auto;
    color: #61728b;
    font-size: 9px;
    white-space: nowrap;
}

.chat-reply {
    margin-left: 3px;
    color: #566983;
    font-size: 12px;
}

.chat-text {
    margin-top: 4px;
    color: #d2d9e5;
    font-size: 12px;
    line-height: 1.4;
    word-break: break-word;
}

.chat-input {

    padding: 10px;

    display: flex;

    gap: 7px;

    border-top:
        1px solid var(--line);
}

.chat-input input {

    width: 100%;

    min-width: 0;

    padding: 11px;

    outline: none;

    border:
        1px solid var(--line);

    border-radius: 9px;

    background: #111c2b;

    color: white;
}

.chat-input button {

    width: 42px;

    border: 0;

    border-radius: 9px;

    background: var(--blue);

    color: white;
}

.banner {

    min-height: 72px;

    padding: 13px 17px;

    display: flex;

    align-items: center;

    gap: 14px;

    border:
        1px solid #3a2e15;

    border-radius: 10px;

    background:
        linear-gradient(
            90deg,
            rgba(65,40,11,.85),
            rgba(19,25,36,.75)
        );
}

.banner-icon {

    width: 45px;
    height: 45px;

    display: grid;

    place-items: center;

    border-radius: 12px;

    background: #e5a71c;

    font-size: 24px;
}

.banner-title {

    color: #f6c342;

    font-size: 11px;

    font-weight: 900;

    letter-spacing: 1px;
}

.banner-sub {

    margin-top: 3px;

    font-weight: 800;
}

.banner button {

    margin-left: auto;

    padding: 10px 17px;

    border: 0;

    border-radius: 9px;

    background: #f59e0b;

    color: #1b1203;

    font-weight: 900;
}

.toolbar {

    margin-top: 14px;

    display: flex;

    align-items: center;

    gap: 8px;
}

.primary {

    padding: 11px 18px;

    border: 0;

    border-radius: 9px;

    background:
        linear-gradient(
            135deg,
            var(--blue),
            #4d79ff
        );

    color: white;

    font-weight: 900;
}

.secondary {

    padding: 10px 15px;

    border:
        1px solid #2d3c53;

    border-radius: 9px;

    background: #132035;

    color: #cdd7e6;

    font-weight: 800;
}

.toolbar-right {

    margin-left: auto;

    display: flex;

    gap: 8px;
}

.toolbar select {

    padding: 10px 12px;

    border:
        1px solid #2d3c53;

    border-radius: 9px;

    background: #142238;

    color: #dbe4f1;
}

.stats {

    margin-top: 12px;

    min-height: 48px;

    padding: 0 14px;

    display: flex;

    align-items: center;

    gap: 25px;

    border:
        1px solid #1c293b;

    border-radius: 9px;

    background: #0d1624;

    color: #aab6c8;

    font-size: 12px;

    font-weight: 800;
}

.match-list {

    margin-top: 6px;
}

.match {

    min-height: 88px;

    margin: 8px 0;

    padding: 13px 15px;

    display: grid;

    grid-template-columns:
        265px
        minmax(100px,1fr)
        150px
        110px
        115px;

    gap: 12px;

    align-items: center;

    border:
        1px solid #202d42;

    border-radius: 12px;

    background: #151e2e;
}

/* Dedicated 1v1v1 layout: three player cards on top, dice in a separate row. */
.game-top.game-top-three {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    grid-template-areas: "creator opponent third" "dice dice dice";
    align-items: start;
    gap: 22px;
    padding: 24px 24px 10px;
}
.game-top-three > #gameCreatorCard { grid-area: creator; }
.game-top-three > #gameOpponentCard { grid-area: opponent; }
.game-top-three > #gameThirdCard { grid-area: third; }
.game-top-three > .roll-center {
    grid-area: dice;
    justify-self: center;
    align-self: start;
    width: 100%;
    max-width: 560px;
    min-width: 0;
    margin: 8px auto 0;
    position: relative;
}
.game-top-three > .game-player-card { min-width: 0; width: 100%; align-self: start; }
.game-top-three .rolls { flex-wrap: wrap; }

/* Enhanced match player cards + live mini dice */
.match .players {
    gap: 12px;
    min-width: 0;
}
.match:has(.player-mini:nth-child(5)) {
    grid-template-columns: minmax(300px, 2fr) minmax(150px, 1fr) 110px 100px 115px;
}
.match .players:has(.player-mini:nth-child(5)) {
    gap: 7px;
}
.match .players:has(.player-mini:nth-child(5)) .avatar,
.match .players:has(.player-mini:nth-child(5)) .waiting-avatar {
    width: 34px;
    height: 34px;
    min-width: 34px;
}
.match .players:has(.player-mini:nth-child(5)) .player-mini-name { max-width: 65px; }


.player-mini {
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1px;
    position: relative;
    cursor: pointer;
}

.player-mini-colors {
    min-height: 7px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 3px;
}

.player-mini-colors .tiny-color {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    box-shadow: 0 0 0 1px rgba(255,255,255,.12);
}

.player-mini-line {
    display: flex;
    align-items: center;
    gap: 6px;
}

.player-mini-name {
    max-width: 88px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 11px;
    font-weight: 950;
}

.winner-card {
    animation: winnerGlow 1.1s ease-in-out infinite alternate;
}

.winner-card .avatar,
.winner-card .big-avatar {
    box-shadow:
        0 0 0 2px #f5c542,
        0 0 18px rgba(245,197,66,.62);
}

.winner-badge {
    position: absolute;
    top: -9px;
    right: -9px;
    z-index: 3;
    padding: 3px 6px;
    border-radius: 999px;
    background: linear-gradient(135deg,#f8d85a,#e99d18);
    color: #241700;
    font-size: 8px;
    font-weight: 1000;
    box-shadow: 0 4px 12px rgba(0,0,0,.35);
}

@keyframes winnerGlow {
    from { box-shadow: inset 0 0 0 rgba(245,197,66,0); }
    to { box-shadow: inset 0 0 35px rgba(245,197,66,.07); }
}

.match-live {
    min-width: 110px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 5px;
}

.match-live-title {
    min-height: 16px;
    color: #91a4bf;
    font-size: 9px;
    font-weight: 1000;
    letter-spacing: .08em;
}

.mini-rolls {
    display: flex;
    align-items: center;
    gap: 7px;
    min-height: 38px;
}

.mini-die {
    width: 32px;
    height: 32px;
    border-radius: 7px;
    background: #2a394e;
    border: 1px solid rgba(255,255,255,.12);
    position: relative;
    transition: transform .12s, filter .12s, background .12s;
}

.mini-die::after {
    content: "";
    width: 8px;
    height: 8px;
    position: absolute;
    left: 50%;
    top: 50%;
    transform: translate(-50%,-50%);
    border-radius: 50%;
    background: rgba(255,255,255,.88);
}

.mini-die.mini-rolling {
    animation: miniDiceShake .10s linear infinite;
    filter: brightness(1.35);
}

@keyframes miniDiceShake {
    0% { transform: rotate(0) translate(0); }
    25% { transform: rotate(6deg) translate(1px,-1px); }
    50% { transform: rotate(-6deg) translate(-1px,1px); }
    75% { transform: rotate(4deg) translate(1px,1px); }
    100% { transform: rotate(0) translate(0); }
}

.live-countdown {
    color: #ffffff;
    font-size: 15px;
    font-weight: 1000;
    animation: liveCount .55s ease;
}

@keyframes liveCount {
    from { transform: scale(.65); opacity: .2; }
    to { transform: scale(1); opacity: 1; }
}

.live-reroll {
    color: #f5c542;
    font-size: 9px;
    font-weight: 1000;
    animation: rerollPulse .55s ease-in-out infinite alternate;
}

.players {

    display: flex;

    align-items: center;

    gap: 9px;

    min-width: 0;
}

.avatar {

    width: 44px;
    height: 44px;

    border-radius: 50%;

    object-fit: cover;

    border:
        2px solid #293a55;
}

.vs {

    color: #77879e;

    font-size: 11px;

    font-weight: 900;
}

.waiting-avatar {

    width: 44px;
    height: 44px;

    display: grid;

    place-items: center;

    border:
        2px solid #2d405e;

    border-radius: 50%;

    background: #0e1726;

    color: #8294ae;
}

.color-row {

    display: flex;

    align-items: center;

    gap: 7px;

    flex-wrap: wrap;
}

.color-mini {

    width: 35px;
    height: 35px;

    position: relative;

    border-radius: 7px;

    border:
        1px solid rgba(255,255,255,.12);
}

.color-mini::after {

    content: "";

    width: 7px;
    height: 7px;

    position: absolute;

    left: 50%;
    top: 50%;

    transform:
        translate(-50%,-50%);

    border-radius: 50%;

    background: white;
}

.wager {

    text-align: center;

    font-size: 17px;

    font-weight: 950;
}

.wager small {

    display: block;

    margin-top: 4px;

    color: #74859e;

    font-size: 9px;

    font-weight: 800;
}

.status {

    text-align: center;
}

.score {

    margin-bottom: 6px;

    color: #83b6ff;

    font-weight: 900;

    font-size: 11px;
}

.dots {

    display: flex;

    justify-content: center;

    gap: 5px;
}

.dot {

    width: 21px;
    height: 21px;

    border-radius: 5px;

    border:
        1px solid rgba(255,255,255,.15);
}

.actions {

    display: flex;

    flex-direction: column;

    gap: 6px;
}

.join,
.callbot,
.cancel,
.view {

    padding: 9px 10px;

    border: 0;

    border-radius: 9px;

    color: white;

    font-weight: 900;
}

.join {

    background: var(--blue);
}

.callbot {

    background: #8b5cf6;
}

.cancel {

    background: #7f1d1d;

    border:
        1px solid #a52a2a;
}

.view {

    background: #27344b;

    border:
        1px solid #35455f;
}

.empty {

    margin-top: 20px;

    padding: 60px;

    text-align: center;

    color: #72819a;

    border:
        1px dashed #26354b;

    border-radius: 12px;
}

.overlay {

    position: fixed;

    inset: 0;

    z-index: 100;

    display: none;

    align-items: center;

    justify-content: center;

    padding: 20px;

    background:
        rgba(2,6,12,.72);

    backdrop-filter:
        blur(8px);
}

.overlay.open {

    display: flex;
}

.modal {

    width: min(940px,100%);

    max-height: 92vh;

    overflow: auto;

    border:
        1px solid #34445e;

    border-radius: 20px;

    background: #151c2d;

    box-shadow:
        0 30px 100px rgba(0,0,0,.55);
}

.modal-head {

    height: 80px;

    padding: 0 25px;

    display: flex;

    align-items: center;

    justify-content: center;

    position: relative;

    border-bottom:
        1px solid #2b3850;
}

.modal-logo {

    font-size: 22px;

    font-weight: 950;
}

.close {

    width: 40px;
    height: 40px;

    position: absolute;

    right: 13px;
    top: 13px;

    border:
        1px solid #34445d;

    border-radius: 11px;

    background: #202a3e;

    color: white;

    font-size: 22px;
}

.create-form {

    padding: 28px;
}

.form-label {

    display: block;

    margin-bottom: 8px;

    color: #95a4bb;

    font-size: 12px;

    font-weight: 900;
}

.amount-input {

    width: 100%;

    padding: 14px;

    outline: none;

    border:
        1px solid #34445d;

    border-radius: 10px;

    background: #0f1726;

    color: white;

    font-size: 16px;
}

.color-count {

    margin-top: 18px;

    display: flex;

    gap: 8px;
}

.count-btn {

    flex: 1;

    padding: 11px;

    border:
        1px solid #33445f;

    border-radius: 9px;

    background: #111c2c;

    color: #aebbd0;

    font-weight: 900;
}

.count-btn.active {

    border-color: #4e8dff;

    background: #1b3969;

    color: white;
}

.color-grid {

    margin-top: 15px;

    display: grid;

    grid-template-columns:
        repeat(3,1fr);

    gap: 10px;
}

.color-option {

    padding: 15px;

    border:
        2px solid transparent;

    border-radius: 12px;

    color: white;

    font-weight: 950;

    text-transform: uppercase;
}

.color-option.selected {

    border-color: white;

    box-shadow:
        0 0 0 3px rgba(255,255,255,.08);
}

.create-submit {

    width: 100%;

    margin-top: 18px;

    padding: 14px;

    border: 0;

    border-radius: 10px;

    background:
        linear-gradient(
            135deg,
            #347ff5,
            #5b6fff
        );

    color: white;

    font-weight: 950;
}

.game-top {

    padding: 25px 40px 5px;

    display: grid;

    grid-template-columns:
        1fr 330px 1fr;

    align-items: center;

    gap: 20px;
}

.player {

    text-align: center;
}

.big-avatar {

    width: 100px;
    height: 100px;

    border-radius: 50%;

    object-fit: cover;

    border:
        2px solid #32425d;
}

.player-name {

    margin-top: 8px;

    font-size: 17px;

    font-weight: 950;
}

.color-tags {

    margin-top: 8px;

    display: flex;

    justify-content: center;

    gap: 5px;

    flex-wrap: wrap;
}

.color-tag {

    display: inline-block;

    padding: 5px 13px;

    border-radius: 99px;

    color: white;

    font-size: 10px;

    font-weight: 950;

    text-transform: capitalize;
}

.roll-center {

    text-align: center;
}

.roll-title {

    margin-bottom: 11px;

    color: #8b99b0;

    letter-spacing: 3px;

    font-size: 10px;

    font-weight: 900;
}

.rolls {

    display: flex;

    justify-content: center;

    gap: 9px;
}

.roll-box {

    width: 65px;
    height: 65px;

    display: grid;

    place-items: center;

    position: relative;

    border-radius: 11px;

    box-shadow:
        inset 0 0 0 1px
        rgba(255,255,255,.08);
}

.roll-box::after {

    content: "";

    width: 15px;
    height: 15px;

    border-radius: 50%;

    background:
        rgba(255,255,255,.92);
}

.winner {

    margin-top: 16px;

    display: inline-block;

    padding: 13px 26px;

    border-radius: 99px;

    background: #16ad61;

    color: white;

    font-weight: 950;
}

.history {

    margin: 25px 28px 0;

    padding: 18px;

    border-radius: 15px;

    background: #0e1524;
}

.history-title {

    margin-bottom: 14px;

    text-align: center;

    font-size: 12px;

    font-weight: 950;
}

.history-row {

    display: flex;

    align-items: center;

    gap: 8px;

    padding: 10px;

    border-radius: 9px;

    background: #141c2d;
}

.game-status {

    min-height: 42px;

    display: flex;

    align-items: center;

    justify-content: center;

    margin-top: 12px;

    color: #aebbd0;

    font-size: 14px;

    font-weight: 950;

    letter-spacing: 2px;
}

.game-countdown {

    font-size: 42px;

    font-weight: 1000;

    color: white;

    text-shadow: 0 0 25px rgba(255,255,255,.25);

    animation: countdownPulse .75s ease;
}

@keyframes countdownPulse {
    0% { transform: scale(.55); opacity: 0; }
    55% { transform: scale(1.18); opacity: 1; }
    100% { transform: scale(1); opacity: 1; }
}

.roll-box.rolling {

    animation: diceShake .12s linear infinite;

    filter: brightness(1.35);

    box-shadow:
        0 0 22px rgba(255,255,255,.12),
        inset 0 0 0 1px rgba(255,255,255,.15);
}

@keyframes diceShake {
    0% { transform: rotate(0deg) translate(0,0); }
    25% { transform: rotate(7deg) translate(2px,-2px); }
    50% { transform: rotate(-7deg) translate(-2px,2px); }
    75% { transform: rotate(5deg) translate(2px,1px); }
    100% { transform: rotate(0deg) translate(0,0); }
}

.roll-box.reveal {
    animation: diceReveal .35s ease-out;
}

@keyframes diceReveal {
    0% { transform: scale(.72); opacity: .25; }
    65% { transform: scale(1.12); opacity: 1; }
    100% { transform: scale(1); opacity: 1; }
}

.three-player-history { display:flex; align-items:center; gap:12px; flex-wrap:wrap; min-height:54px; }
.history-dice { display:flex; gap:7px; align-items:center; }
.history-dice .color-mini { width:38px; height:38px; border-radius:8px; display:inline-block; position:relative; }
.history-dice .color-mini::after { content:""; position:absolute; width:8px; height:8px; border-radius:50%; background:#fff; left:50%; top:50%; transform:translate(-50%,-50%); }
.three-player-history .history-score { margin-left:auto; max-width:60%; text-align:right; }
@media (max-width: 760px) {
    .game-top.game-top-three {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        grid-template-areas: "creator opponent" "third third" "dice dice";
        gap: 18px 12px;
        padding: 18px 12px 8px;
    }
    .game-top-three > .roll-center { margin-top: 4px; }
    .game-top-three .big-avatar { width: 76px; height: 76px; }
    .three-player-history .history-score { max-width:100%; width:100%; text-align:left; margin-left:0; }
}

.history-row.new-round {
    animation: historyIn .35s ease-out;
}

@keyframes historyIn {
    from { transform: translateY(8px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
}

.history-score {
    margin-left: auto;
    padding: 6px 11px;
    border-radius: 8px;
    background: #1c2940;
    color: white;
    font-size: 12px;
    font-weight: 950;
}

.reroll-text {
    color: #f5c542;
    animation: rerollPulse .7s ease-in-out infinite alternate;
}

@keyframes rerollPulse {
    from { opacity: .55; }
    to { opacity: 1; }
}

.history-num {

    width: 28px;

    color: #71819b;

    font-size: 11px;
}

.modal-bottom {

    padding: 20px 30px 30px;

    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 25px;
}

.wager-block label {

    display: block;

    color: #8492aa;

    font-size: 11px;
}

.wager-value {

    margin-top: 6px;

    font-size: 20px;

    font-weight: 950;
}

.profile-card {
    display: flex;
    flex-direction: column;
    gap: 18px;
}

.profile-hero {
    display: flex;
    align-items: center;
    gap: 14px;
}

.profile-avatar-wrap {
    position: relative;
    width: 70px;
    height: 70px;
}

.profile-avatar {
    width: 70px;
    height: 70px;
    border-radius: 18px;
    object-fit: cover;
    border: 2px solid #2e415e;
}

.profile-level {
    position: absolute;
    left: -4px;
    bottom: -7px;
    padding: 3px 7px;
    border-radius: 6px;
    background: #14b8d4;
    color: #07141b;
    border: 2px solid #151c2d;
    font-size: 9px;
    font-weight: 1000;
}

.profile-name {
    color: #55d8ef;
    font-size: 18px;
    font-weight: 1000;
}

.profile-username {
    margin-top: 4px;
    color: #71839d;
    font-size: 11px;
    font-weight: 700;
}

.profile-stats {
    display: grid;
    grid-template-columns: repeat(5,1fr);
    gap: 8px;
}

.profile-stats > div {
    padding: 12px 8px;
    text-align: center;
    border: 1px solid #25354d;
    border-radius: 10px;
    background: #101a29;
}

.profile-stats b {
    display: block;
    color: white;
    font-size: 13px;
    font-weight: 1000;
}

.profile-stats span {
    display: block;
    margin-top: 4px;
    color: #71839d;
    font-size: 9px;
    font-weight: 800;
}

.profile-history-title {
    color: #8fa2bd;
    font-size: 11px;
    font-weight: 1000;
    letter-spacing: .08em;
}

.profile-history {
    display: flex;
    flex-direction: column;
    gap: 7px;
}

.history-card {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 12px;
    border: 1px solid #25354d;
    border-radius: 10px;
    background: #101a29;
}

.history-card-user {
    display: flex;
    align-items: center;
    gap: 9px;
}

.history-card-user img {
    width: 32px;
    height: 32px;
    border-radius: 8px;
    object-fit: cover;
}

.history-card small,
.history-card-user small {
    display: block;
    margin-top: 2px;
    color: #687b96;
    font-size: 9px;
}

.history-win {
    color: #28d68d !important;
    font-weight: 1000;
}

.history-loss {
    color: #f06b73 !important;
    font-weight: 1000;
}

.toast {

    position: fixed;

    left: 50%;
    bottom: 25px;

    transform:
        translateX(-50%)
        translateY(20px);

    opacity: 0;

    pointer-events: none;

    z-index: 300;

    padding: 12px 18px;

    border-radius: 10px;

    background: #18253a;

    border:
        1px solid #34465f;

    font-weight: 800;

    transition: .2s;
}

.toast.show {

    opacity: 1;

    transform:
        translateX(-50%)
        translateY(0);
}

@media (max-width: 720px) {
    .profile-stats {
        grid-template-columns: repeat(2, 1fr);
    }
}

@media(max-width:1200px) {

    .layout {

        grid-template-columns:
            68px
            minmax(0,1fr);
    }

    .chat {

        display: none;
    }

    .match {

        grid-template-columns:
            230px
            minmax(80px,1fr)
            120px
            100px
            100px;
    }
}

@media(max-width:850px) {

    .brand {

        width: auto;
    }

    .brand-text {

        display: none;
    }

    .layout {

        grid-template-columns:
            58px
            minmax(0,1fr);
    }

    .sidebar {

        width: 58px;
    }

    .main {

        padding: 12px;
    }

    .banner {

        display: none;
    }

    .stats {

        overflow-x: auto;

        white-space: nowrap;
    }

    .match {

        grid-template-columns:
            1fr 1fr;
    }

    .players,
    .color-row,
    .wager,
    .status {

        grid-column:
            span 1;
    }

    .actions {

        grid-column:
            1 / -1;

        flex-direction: row;
    }

    .actions button {

        flex: 1;
    }

    .game-top {

        grid-template-columns:
            1fr;
    }

    .modal-bottom {

        grid-template-columns:
            1fr;
    }
}


/* Shuffle-inspired casino lobby (original Colordice styling) */
.lobby-hero{position:relative;overflow:hidden;display:grid;grid-template-columns:minmax(0,1.2fr) minmax(240px,.8fr);gap:20px;align-items:center;padding:30px;border:1px solid #293854;border-radius:20px;background:radial-gradient(circle at 80% 25%,rgba(120,73,255,.35),transparent 34%),linear-gradient(120deg,#172742,#111a2d 65%,#20163a);margin-bottom:22px;min-height:210px}
.lobby-kicker{font-size:11px;font-weight:900;letter-spacing:.15em;color:#9db9ff;text-transform:uppercase;margin-bottom:10px}
.lobby-hero h1{font-size:clamp(26px,3vw,40px);line-height:1.08;margin:0 0 12px;font-weight:1000;letter-spacing:-.04em}
.lobby-hero p{margin:0 0 18px;color:#aebbd0;max-width:460px;line-height:1.55}
.lobby-cta{display:flex;gap:10px;flex-wrap:wrap}
.lobby-cta button{border:0;border-radius:10px;padding:12px 17px;font-weight:900;color:white;background:linear-gradient(135deg,#477dff,#7749f5)}
.lobby-cta .lobby-secondary{background:#202e47;border:1px solid #344563}
.lobby-hero-art{display:flex;align-items:center;justify-content:center;gap:12px;min-height:130px;transform:rotate(-7deg)}
.lobby-die{width:86px;height:86px;border-radius:22px;display:grid;place-items:center;font-size:52px;background:linear-gradient(145deg,#f6f8ff,#bfcdf7);color:#25355a;box-shadow:0 18px 40px #05091499;border:1px solid white}
.lobby-die:nth-child(2){transform:translateY(-22px) rotate(14deg);background:linear-gradient(145deg,#c8b7ff,#7957ed);color:white}
.lobby-die:nth-child(3){transform:translateY(18px) rotate(19deg);background:linear-gradient(145deg,#a5f3d0,#20a875);color:#073c2d}
.lobby-cats{display:flex;gap:8px;overflow-x:auto;padding:2px 0 17px;margin-bottom:4px}
.lobby-cat{white-space:nowrap;border:1px solid #283852;background:#101a2b;color:#aebbd0;border-radius:10px;padding:10px 15px;font-weight:800}
.lobby-cat.active,.lobby-cat:hover{background:#233b60;border-color:#4d7fe4;color:#fff}
.lobby-section{margin:18px 0 24px}
.lobby-section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.lobby-section-head h2{font-size:18px;margin:0;font-weight:950}
.lobby-section-head button{border:0;background:transparent;color:#91b5ff;font-weight:800}
.lobby-games{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.lobby-game{position:relative;overflow:hidden;min-height:150px;padding:15px;border:1px solid #293852;border-radius:15px;background:linear-gradient(145deg,#202f4c,#121c2e);cursor:pointer;transition:transform .16s,border-color .16s}
.lobby-game:hover{transform:translateY(-3px);border-color:#648cff}
.lobby-game .game-art{height:76px;display:grid;place-items:center;font-size:53px;filter:drop-shadow(0 8px 12px #0008)}
.lobby-game h3{font-size:14px;margin:7px 0 3px}
.lobby-game p{font-size:11px;color:#9aaac2;margin:0}
.lobby-game.purple{background:radial-gradient(circle at 80% 10%,#6740a9,transparent 55%),linear-gradient(145deg,#2d214b,#121c2e)}
.lobby-game.green{background:radial-gradient(circle at 80% 10%,#207a60,transparent 55%),linear-gradient(145deg,#183d3a,#121c2e)}
.lobby-game.orange{background:radial-gradient(circle at 80% 10%,#9c5728,transparent 55%),linear-gradient(145deg,#4a3028,#121c2e)}
.lobby-game.blue{background:radial-gradient(circle at 80% 10%,#285e9d,transparent 55%),linear-gradient(145deg,#1b3556,#121c2e)}
.lobby-live-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:#aab8cd;font-size:12px}
.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#2be39a;box-shadow:0 0 10px #2be39a88}
@media(max-width:1100px){.lobby-games{grid-template-columns:repeat(2,minmax(0,1fr))}.lobby-hero{grid-template-columns:1fr}.lobby-hero-art{display:none}}
@media(max-width:650px){.lobby-hero{padding:22px;min-height:0}.lobby-games{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.lobby-game{min-height:130px;padding:11px}.lobby-game .game-art{height:62px;font-size:43px}.lobby-cat{padding:9px 12px}}
/* Landing lobby is shown first; the existing ColorDice app opens from its game card. */
#gameApp{display:none}.game-app-heading{display:flex;align-items:center;gap:14px;margin:0 0 18px;font-weight:900;font-size:18px}.back-lobby-btn{border:1px solid #344563;background:#18253a;color:#eaf0ff;border-radius:9px;padding:9px 13px;font-weight:800;cursor:pointer}.lobby-games{grid-template-columns:repeat(4,minmax(0,1fr))}.lobby-game{min-height:190px}.lobby-game .game-art{height:105px;font-size:68px}.game-play-pill{display:inline-block;margin-top:12px;border-radius:7px;padding:6px 9px;background:#7257f5;color:white;font-size:10px;font-weight:950;letter-spacing:.08em}.lobby-win-empty{border:1px dashed #33425d;background:#111b2c;border-radius:12px;padding:20px;color:#9cabc3;font-size:13px}
.lobby-wins-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.lobby-win-card{display:flex;align-items:center;gap:10px;min-width:0;padding:12px;border:1px solid #293852;border-radius:12px;background:linear-gradient(145deg,#17263a,#111a2a)}
.lobby-win-avatar{width:38px;height:38px;flex:0 0 38px;border-radius:50%;object-fit:cover;background:#273651;display:grid;place-items:center;font-size:18px}
.lobby-win-info{min-width:0;flex:1}.lobby-win-name{font-size:12px;font-weight:900;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#eef4ff}.lobby-win-detail{font-size:10px;color:#91a2bd;margin-top:4px}.lobby-win-payout{font-size:12px;font-weight:950;color:#39e6a0;white-space:nowrap}
@media(max-width:800px){.lobby-wins-list{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:520px){.lobby-wins-list{grid-template-columns:1fr}}
.live-label{font-size:10px;letter-spacing:.12em;color:#8d9fbb;font-weight:900}@media(max-width:650px){.lobby-games{grid-template-columns:repeat(2,minmax(0,1fr))}.lobby-game{min-height:160px}.lobby-game .game-art{height:82px;font-size:55px}}


\n/* ============================================================\n   Pet Simulator 99 item UI\n   ============================================================ */\n.top-wallet-center{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:flex;align-items:center;justify-content:center;gap:8px;z-index:4;white-space:nowrap}.top-gem-balance{height:42px;padding:0 15px;display:flex;align-items:center;gap:7px;border:1px solid #293852;border-radius:10px;background:#101b2c;color:#eef5ff}.top-gem-balance strong{font-size:15px}.gem-icon{font-size:15px}.inventory-create-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:10px}.inventory-create-actions{display:flex;align-items:center;gap:8px;flex:1;justify-content:flex-end}.inventory-search{width:260px;padding:11px 13px;border:1px solid #2a3b54;border-radius:9px;background:#0b1624;color:white;outline:none}.inventory-sort{padding:11px 12px;border:1px solid #2a3b54;border-radius:9px;background:#132238;color:#dce6f4}.inventory-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;max-height:310px;overflow:auto;padding:4px}.inventory-item{position:relative;min-width:0;padding:8px;border:1px solid #263751;border-radius:10px;background:#101b2c;color:#eaf1ff;cursor:pointer;text-align:left;transition:.15s}.inventory-item:hover{border-color:#4c82f5;transform:translateY(-1px)}.inventory-item.selected{border-color:#4c82f5;box-shadow:0 0 0 2px rgba(76,130,245,.25);background:#172744}.inventory-item img{width:100%;aspect-ratio:1/1;object-fit:contain;border-radius:7px;background:#0b1421}.inventory-item-name{font-size:10px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:5px}.inventory-item-value{font-size:10px;color:#58d6ff;font-weight:900;margin-top:2px}.inventory-item-count{position:absolute;top:6px;right:6px;background:#111827dd;border:1px solid #3a4b64;border-radius:6px;padding:2px 5px;font-size:9px;font-weight:900}.inventory-empty{border:1px dashed #30435e;border-radius:12px;padding:35px;text-align:center;color:#8c9bb1;grid-column:1/-1}.match-item-battle{display:flex;align-items:center;justify-content:center;gap:10px;min-width:0}.match-item-side{display:flex;align-items:center;gap:4px;min-width:0;max-width:170px}.match-item-side img{width:38px;height:38px;object-fit:contain;border-radius:8px;background:#0c1625;border:1px solid #2a3a52}.match-item-more{font-size:10px;color:#8ea0ba;font-weight:900}.match-item-vs{font-size:9px;color:#72849e;font-weight:1000;letter-spacing:.08em}.match-item-total{font-size:11px;color:#69d7ff;font-weight:950;text-align:center;margin-top:3px}.match-item-total small{display:block;color:#7587a0;font-size:8px;margin-top:1px}.profile-action-row{display:flex;justify-content:flex-end;gap:8px;margin:12px 0}.item-give-grid{max-height:360px}.give-code-box{display:flex;gap:8px;margin-top:12px}.give-code-box input{flex:1;padding:11px;border:1px solid #2a3b54;border-radius:9px;background:#0b1624;color:white}.wallet-panel{background:#0c1826;border:1px solid #263852;border-radius:12px;padding:12px}.wallet-panel-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}.wallet-value{font-size:28px;font-weight:1000}.wallet-sub{color:#8798b0;font-size:11px}.wallet-toolbar{display:flex;gap:8px;margin-bottom:10px}.wallet-toolbar input{flex:1;padding:10px;border:1px solid #2a3b54;border-radius:9px;background:#09131f;color:white}.wallet-items{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;max-height:390px;overflow:auto}.wallet-item-card{border:1px solid #263751;border-radius:10px;background:#111d2d;padding:7px;text-align:center}.wallet-item-card img{width:100%;aspect-ratio:1/1;object-fit:contain}.wallet-item-card b{display:block;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.wallet-item-card span{font-size:9px;color:#63d7ff;font-weight:900}.match-wager-items{display:flex;align-items:center;justify-content:center;gap:5px;min-height:48px}.match-wager-items img{width:42px;height:42px;object-fit:contain;border-radius:8px;border:1px solid #2a3b52;background:#0c1625}.match-wager-items .more{font-size:10px;color:#8496af;font-weight:900}.match-wager-value{font-size:11px;color:#69d7ff;font-weight:950;margin-top:3px}.match-wager-value small{display:block;color:#7d8da5;font-size:8px}.match-live-items{min-width:150px}\n@media(max-width:900px){.top-wallet-center{position:static;transform:none;order:2}.topbar{flex-wrap:wrap}.top-actions{order:3;width:100%;justify-content:flex-end}.inventory-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.wallet-items{grid-template-columns:repeat(4,minmax(0,1fr))}.inventory-create-head{flex-direction:column;align-items:stretch}.inventory-create-actions{justify-content:stretch}.inventory-search{width:auto;flex:1}}\n

/* Exact PS99-style create modal layout */
.create-overlay{padding:20px;background:rgba(2,6,12,.78);backdrop-filter:blur(10px)}
.create-modal{width:min(1080px,calc(100vw - 40px));height:min(650px,calc(100vh - 40px));display:flex;flex-direction:column;overflow:hidden;border:1px solid #31425c;border-radius:17px;background:#111b2b;box-shadow:0 30px 100px rgba(0,0,0,.65)}
.create-topbar{height:68px;flex:0 0 68px;display:flex;align-items:center;gap:9px;padding:0 14px;border-bottom:1px solid #26364d;background:#132235}
.create-search-wrap{width:315px;height:40px;position:relative}
.create-search-icon{position:absolute;left:14px;top:8px;color:#7e91aa;font-size:22px;line-height:24px;pointer-events:none}
.create-search{width:100%;height:40px;padding:0 13px 0 39px;border:1px solid #2c4059;border-radius:9px;background:#0b1725;color:#eef5ff;outline:none;font:inherit}
.create-search::placeholder{color:#71839c}
.create-sort-btn,.create-category{height:40px;border:1px solid #2b3e57;border-radius:9px;background:#15263a;color:#dbe6f5;padding:0 13px;font-weight:800;font-size:11px}
.create-sort-btn{margin-left:auto;cursor:pointer}.create-category{cursor:pointer;min-width:130px}
.create-close{width:40px;height:40px;border:1px solid #2d4059;border-radius:9px;background:#142337;color:#aebed1;font-size:25px;cursor:pointer}
.create-wagerbar{height:68px;flex:0 0 68px;display:flex;align-items:center;justify-content:space-between;padding:0 18px;border:1px solid #26384f;border-radius:10px;margin:12px 12px 0;background:#0d1b29}
.create-mini-label,.create-bottom-label{font-size:9px;letter-spacing:.14em;color:#8295ae;font-weight:950;text-transform:uppercase}.create-wager-summary{display:flex;align-items:center;gap:5px;margin-top:3px;color:#eef5ff;font-size:13px}.create-summary-dot{color:#667993}.create-balance{display:flex;align-items:center;gap:10px;color:#8295ae;font-size:10px}.create-balance b{color:#edf5ff;font-size:13px}.create-gem-input{height:35px;min-width:143px;padding:0 11px;display:flex;align-items:center;border:1px solid #26384f;border-radius:9px;background:#101c2c;color:#667a95}.create-balance button{border:0;background:none;color:#8ea1ba;font-weight:900;font-size:10px;cursor:pointer}
.create-inventory-shell{flex:1;min-height:0;margin:12px;border:1px dashed #2b405b;border-radius:13px;background:#101a2b;overflow:hidden}.create-inventory-grid{height:100%;max-height:none;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:9px;padding:12px;overflow:auto;align-content:start}.create-inventory-grid:has(.inventory-empty){display:grid;align-content:center}.create-inventory-grid .inventory-empty{min-height:285px;display:flex;flex-direction:column;align-items:center;justify-content:center;border:0;background:transparent;padding:20px;color:#8c9ab0;font-size:13px}.create-inventory-grid .inventory-empty:before{content:'♧';width:50px;height:50px;display:grid;place-items:center;margin-bottom:13px;border:1px solid #2b4a71;border-radius:15px;color:#4f9cff;font-size:25px;background:#102640}.create-inventory-grid .inventory-item{background:#111f31}.create-bottom{min-height:84px;flex:0 0 84px;display:flex;align-items:flex-end;gap:16px;padding:0 14px 14px}.create-bottom-group{display:flex;flex-direction:column;gap:7px}.create-segment{display:flex;height:36px;border:1px solid #2b3d57;border-radius:12px;padding:3px;background:#17253a}.create-segment-btn{min-width:39px;border:0;border-radius:8px;background:transparent;color:#8798ae;font-size:10px;font-weight:950;cursor:pointer}.create-segment-btn.active{background:#397cf4;color:#fff;box-shadow:0 3px 10px rgba(51,125,245,.3)}.create-color-row{display:flex;align-items:center;gap:8px}.create-color-count{height:36px}.create-color-count .create-segment-btn{min-width:38px}.create-color-swatches{display:flex;gap:7px}.create-color-swatch{width:36px;height:36px;padding:0;border:2px solid transparent;border-radius:9px;cursor:pointer;box-shadow:none}.create-color-swatch.selected{border-color:#fff;box-shadow:0 0 0 2px rgba(255,255,255,.18)}.create-bottom-actions{margin-left:auto;display:flex;align-items:center;gap:8px}.create-rule-btn,.create-select-btn{height:38px;padding:0 15px;border:1px solid #2c3e57;border-radius:10px;background:#142236;color:#8fa0b8;font-size:10px;font-weight:950;cursor:pointer}.create-final-btn{height:38px;min-width:145px;padding:0 16px;border:0;border-radius:10px;background:linear-gradient(135deg,#367cf5,#4d7cff);color:#fff;font-size:11px;font-weight:950;cursor:pointer}.create-final-btn:disabled{opacity:.45;cursor:not-allowed}
@media(max-width:900px){.create-modal{height:min(720px,calc(100vh - 20px));width:calc(100vw - 20px)}.create-topbar{flex-wrap:wrap;height:auto;min-height:68px;padding:10px}.create-search-wrap{width:45%}.create-wagerbar{margin:10px}.create-inventory-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.create-bottom{overflow-x:auto;align-items:flex-end}.create-bottom-actions{margin-left:0}.create-category{display:none}}
@media(max-width:620px){.create-search-wrap{width:100%}.create-sort-btn{margin-left:0}.create-wagerbar{height:auto;min-height:68px}.create-balance{display:none}.create-inventory-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.create-bottom{gap:10px}.colors-bottom-group{min-width:260px}}

</style>

</head>

<body>

<header class="topbar">

    <div class="brand" onclick="showLobby()" style="cursor:pointer" title="Back to lobby">

        <div class="brand-mark">
            🎲
        </div>

        <span class="brand-text">
            ColorDice
        </span>

    </div>

    <div class="top-wallet-center">
        {% if user %}
        <div class="top-gem-balance">💎 <strong id="topBalance">{{ gem_value(user["balance"]) }}</strong></div>
        <button class="wallet-btn" onclick="showWallet()">▣ Wallet</button>
        {% endif %}
    </div>

    <div class="top-actions">

        {% if user %}

        <div class="user-chip">

            <img
                src="{{ user['avatar'] }}"
            >

            <span>
                {{ user["display_name"] }}
            </span>

        </div>

        <button
            class="secondary"
            onclick="location.href='/logout'"
        >
            Logout
        </button>

        {% else %}

        <a href="/login">

            <button class="login-btn">
                Login with Discord
            </button>

        </a>

        {% endif %}

    </div>

</header>


<div class="layout">

<aside class="sidebar">

    <button
        class="nav-btn active"
        onclick="window.scrollTo(0,0)"
    >
        ⌂
    </button>

    <button
        class="nav-btn"
        onclick="enterGame();openCreate()"
    >
        🎲
    </button>

    <button
        class="nav-btn"
        onclick="showHistory()"
    >
        ◷
    </button>

    <button
        class="nav-btn"
        onclick="showLeaderboard()"
    >
        ♛
    </button>

    <button
        class="nav-btn"
        onclick="showProfile()"
    >
        ●
    </button>

    <button class="nav-btn">
        ⚙
    </button>

</aside>


<main class="main">

    <div id="lobbyHome">
    <section class="lobby-hero">
        <div>
            <div class="lobby-kicker">● COLOR DICE ORIGINALS</div>
            <h1>Make every roll<br>count.</h1>
            <p>Challenge other players, choose your colors, and climb the Colordice leaderboard.</p>
            <div class="lobby-cta">
                <button onclick="openCreate()">🎲 Play ColorDice</button>
                <button class="lobby-secondary" onclick="showLeaderboard()">♛ Leaderboard</button>
            </div>
        </div>
        <div class="lobby-hero-art" aria-hidden="true">
            <div class="lobby-die">⚄</div><div class="lobby-die">⚅</div><div class="lobby-die">⚂</div>
        </div>
    </section>

    <nav class="lobby-cats" aria-label="Game categories">
        <button class="lobby-cat active" onclick="document.getElementById('lobby-originals').scrollIntoView({behavior:'smooth'})">⌂ Lobby</button>
        <button class="lobby-cat" onclick="document.getElementById('lobby-originals').scrollIntoView({behavior:'smooth'})">🎲 Originals</button>
        <button class="lobby-cat" onclick="document.getElementById('matchList').scrollIntoView({behavior:'smooth'})">⚡ Live Matches</button>
        <button class="lobby-cat" onclick="showHistory()">◷ Recent Matches</button>
        <button class="lobby-cat" onclick="showLeaderboard()">♛ Leaderboard</button>
    </nav>

    <section class="lobby-section" id="lobby-originals">
        <div class="lobby-section-head"><h2>🎮 Colordice Originals</h2><button onclick="openCreate()">View all →</button></div>
        <div class="lobby-games">
            <article class="lobby-game purple" onclick="enterGame()" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();enterGame()}">
                <div class="game-art">🎲</div><h3>ColorDice</h3><p>Choose your color · Roll to win</p>
                <span class="game-play-pill">PLAY NOW ↗</span>
            </article>
        </div>
    </section>

    <section class="lobby-section live-wins-section">
        <div class="lobby-section-head"><h2><span class="live-dot"></span> Live Wins</h2><span class="live-label">RECENT ACTIVITY</span></div>
        <div id="lobbyLiveWins" class="lobby-wins-list"><div class="lobby-win-empty">🎲 Loading recent wins…</div></div>
    </section>
    </div><!-- /lobbyHome -->

    <div id="gameApp">
    <div class="game-app-heading"><button class="back-lobby-btn" onclick="showLobby()">← Lobby</button><span>ColorDice Game</span></div>
    <div class="toolbar">

        <button
            class="primary"
            onclick="openCreate()"
        >
            ＋ Create
        </button>

        <button
            class="secondary"
            onclick="showHistory()"
        >
            ◷ History
        </button>

        <div class="toolbar-right">

            <button
                class="secondary"
                onclick="loadMatches()"
            >
                ↻
            </button>

            <select
                id="filter"
                onchange="loadMatches()"
            >
                <option value="all">
                    All Matches
                </option>

                <option value="waiting">
                    Waiting Only
                </option>

                <option value="finished">
                    Finished
                </option>
            </select>

            <select
                id="sort"
                onchange="loadMatches()"
            >
                <option value="high">
                    Highest to Lowest
                </option>

                <option value="low">
                    Lowest to Highest
                </option>
            </select>

        </div>

    </div>


    <div class="stats">

        <span>
            ◈
            <b id="activeCount">
                0
            </b>
            Active Rooms
        </span>

        <span>
            💎
            <b id="totalValue">
                0
            </b>
            Total Value
        </span>

        <span>
            🎲
            <b id="totalGames">
                0
            </b>
            Total Matches
        </span>

    </div>


    <div
        id="matchList"
        class="match-list"
    ></div>

    </div><!-- /gameApp -->
</main>


<aside class="chat">

    <div class="chat-head">

        <span>
            Live Chat
        </span>

        <span class="online">
            ● online
        </span>

    </div>

    <div
        id="chatList"
        class="chat-list"
    ></div>

    <div class="chat-input">

        <input
            id="chatInput"
            maxlength="250"
            placeholder="{% if user %}Send a message...{% else %}Login to chat{% endif %}"
            {% if not user %}disabled{% endif %}
            onkeydown="
                if(event.key==='Enter')
                sendChat()
            "
        >

        <button onclick="sendChat()">
            ➤
        </button>

    </div>

</aside>

</div>


<!-- CREATE -->

<div
    id="createOverlay"
    class="overlay create-overlay"
    onclick="outsideClose(event,'createOverlay')"
>

<div
    class="create-modal"
    onclick="event.stopPropagation()"
>

    <div class="create-topbar">
        <div class="create-search-wrap">
            <span class="create-search-icon">⌕</span>
            <input
                id="createItemSearch"
                class="create-search"
                placeholder="Search for an item..."
                oninput="renderCreateInventory()"
            >
        </div>

        <button class="create-sort-btn" type="button" onclick="toggleCreateSort()">
            ↕ <span id="createSortLabel">Highest to Lowest</span>
        </button>
        <select id="createItemSort" style="display:none"><option value="high">Highest to Lowest</option><option value="low">Lowest to Highest</option></select>

        <select id="createCategory" class="create-category" onchange="renderCreateInventory()">
            <option value="all">All categories</option>
            <option value="pets">Pets</option>
        </select>

        <button
            class="create-close"
            type="button"
            onclick="closeModal('createOverlay')"
        >
            ×
        </button>
    </div>

    <div class="create-wagerbar">
        <div>
            <div class="create-mini-label">YOUR WAGER</div>
            <div class="create-wager-summary">
                <span>💎</span>
                <b id="createWagerValue">0</b>
                <span class="create-summary-dot">·</span>
                <b id="createWagerCount">0 pets</b>
            </div>
        </div>

        <div class="create-balance">
            <span>Balance</span>
            <b>💎 <span id="createBalanceValue">0</span></b>
            <div class="create-gem-input">Gem value</div>
            <button type="button" onclick="selectAllCreateItems()">Max</button>
        </div>
    </div>

    <div class="create-inventory-shell">
        <div id="createInventoryGrid" class="create-inventory-grid"></div>
    </div>

    <div class="create-bottom">
        <div class="create-bottom-group">
            <div class="create-bottom-label">MATCH</div>
            <div class="create-segment">
                <button id="mode1v1Btn" class="create-segment-btn active" onclick="setMatchMode('1v1')">1V1</button>
                <button id="mode1v1v1Btn" class="create-segment-btn" onclick="setMatchMode('1v1v1')">1V1V1</button>
            </div>
        </div>

        <div class="create-bottom-group">
            <div class="create-bottom-label">FIRST TO</div>
            <div class="create-segment">
                <button id="bestOf1Btn" class="create-segment-btn active" onclick="setBestOf(1)">1</button>
                <button id="bestOf2Btn" class="create-segment-btn" onclick="setBestOf(2)">2</button>
                <button id="bestOf3Btn" class="create-segment-btn" onclick="setBestOf(3)">3</button>
            </div>
        </div>

        <div class="create-bottom-group colors-bottom-group">
            <div class="create-bottom-label">PICK 1 OR 2 COLORS</div>
            <div class="create-color-row">
                <div class="create-segment create-color-count">
                    <button id="oneColorBtn" class="create-segment-btn active" onclick="setColorCount(1)">1</button>
                    <button id="twoColorBtn" class="create-segment-btn" onclick="setColorCount(2)">2</button>
                </div>

                <div class="create-color-swatches">
                    {% for name, hex in colors.items() %}
                    <button
                        class="create-color-swatch color-option"
                        data-color="{{ name }}"
                        title="{{ name }}"
                        style="background:{{ hex }}"
                        onclick="selectColor('{{ name }}',this)"
                    ></button>
                    {% endfor %}
                </div>
            </div>
        </div>

        <div class="create-bottom-actions">
            <button type="button" class="create-rule-btn" onclick="showCreateRules()">☷ Rules</button>
            <button type="button" class="create-select-btn" onclick="selectAllCreateItems()">Select All</button>
            <button type="button" id="createSubmitBtn" class="create-final-btn" onclick="createMatch()">
                Create <span id="createSubmitValue">💎 0</span>
            </button>
        </div>
    </div>

</div>
</div>


<!-- GAME -->

<div
    id="gameOverlay"
    class="overlay"
    onclick="outsideClose(event,'gameOverlay')"
>

<div
    class="modal"
    onclick="event.stopPropagation()"
>

    <div class="modal-head">

        <div class="modal-logo">
            🎲 Color Dice
        </div>

        <button
            class="close"
            onclick="closeModal('gameOverlay')"
        >
            ×
        </button>

    </div>

    <div id="gameContent"></div>

</div>

</div>


<!-- GENERAL -->

<div
    id="generalOverlay"
    class="overlay"
    onclick="outsideClose(event,'generalOverlay')"
>

<div
    class="modal"
    onclick="event.stopPropagation()"
>

    <div class="modal-head">

        <div
            id="generalTitle"
            class="modal-logo"
        >
            ColorDice
        </div>

        <button
            class="close"
            onclick="closeModal('generalOverlay')"
        >
            ×
        </button>

    </div>

    <div
        id="generalContent"
        style="padding:28px;"
    ></div>

</div>

</div>


<div
    id="toast"
    class="toast"
></div>


<script>

const COLORS =
    {{ colors|tojson }};

let selectedColors = [];
let selectedItemIds = [];
let inventoryItems = [];
let pendingJoinItemIds = [];
let pendingJoinInventory = [];

let selectedColorCount = 1;
let selectedMatchMode = "1v1";
let selectedBestOf = 1;
let pendingJoinMatchId = null;
let pendingJoinColors = [];
let previousMatchStatuses = {};

let currentMatchId = null;
const activeGameAnimations = new Set();

// Live match state.
// These MUST be declared so the live-update timer can run.
let liveMatches = [];
let matchAnimationTimer = null;
let lastMatchListSignature = "";


function escapeHtml(value) {

    return String(value ?? "")
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
}


function money(value) {
    const n = Number(value || 0);
    const units = [[1e12,"T"],[1e9,"B"],[1e6,"M"],[1e3,"K"]];
    for (const [threshold,suffix] of units) {
        if (n >= threshold) {
            return "💎 " + (n/threshold).toFixed(2).replace(/\.00$/,'').replace(/(\.\d)0$/,'$1') + suffix;
        }
    }
    return "💎 " + n.toLocaleString("en-US", {maximumFractionDigits:2});
}


function toast(message) {

    const el =
        document.getElementById("toast");

    el.textContent = message;

    el.classList.add("show");

    clearTimeout(
        window.__toastTimer
    );

    window.__toastTimer =
        setTimeout(
            () =>
                el.classList.remove("show"),
            2600
        );
}


async function loadInventory(sync=false) {
    try {
        const response = await fetch(`/api/inventory${sync ? "?sync=1" : ""}`, {credentials:"same-origin", cache:"no-store"});
        const data = await response.json();
        if (!response.ok || !data.ok) { if (data.error) toast(data.error); inventoryItems=[]; return data; }
        inventoryItems=data.items||[]; renderCreateInventory(); updateWagerSummary(); return data;
    } catch(e) { console.error("INVENTORY ERROR",e); toast("Could not load inventory."); return null; }
}

async function syncInventory() {
    await showVirtualDeposit();
}

async function showVirtualDeposit(){
    const data=await fetch("/api/catalog",{credentials:"same-origin",cache:"no-store"}).then(r=>r.json()).catch(()=>({}));
    if(!data.ok){toast(data.error||"Could not load PS99 catalog.");return;}
    window.__virtualCatalog=data.items||[];
    showGeneral("Deposit Items",`
      <div class="wallet-panel">
        <div class="wallet-toolbar">
          <input id="depositSearch" placeholder="Search for a pet..." oninput="renderVirtualDeposit()">
        </div>
        <div class="wallet-items" id="virtualDepositGrid"></div>
      </div>`);
    renderVirtualDeposit();
}

function renderVirtualDeposit(){
    const grid=document.getElementById("virtualDepositGrid"); if(!grid)return;
    const q=(document.getElementById("depositSearch")?.value||"").trim().toLowerCase();
    const list=(window.__virtualCatalog||[]).filter(x=>!q||String(x.name).toLowerCase().includes(q)||String(x.item_id).toLowerCase().includes(q)).slice(0,500);
    grid.innerHTML=list.length?list.map(x=>`<button type="button" class="inventory-item" onclick="depositVirtualItem('${escapeHtml(x.item_id).replaceAll("'","\\'")}')"><img src="${escapeHtml(x.icon||"")}" onerror="this.style.opacity=.2"><div class="inventory-item-name">${escapeHtml(x.name)}</div><div class="inventory-item-value">💎 ${escapeHtml(formatCompact(x.value))}</div></button>`).join(""):"<div class=inventory-empty>No pets found.</div>";
}

async function depositVirtualItem(itemId){
    const item=(window.__virtualCatalog||[]).find(x=>x.item_id===itemId); if(!item)return;
    const raw=prompt(`How many ${item.name} do you want to add?`,"1"); if(raw===null)return;
    const quantity=Number(raw); if(!Number.isInteger(quantity)||quantity<1||quantity>1000){toast("Quantity must be between 1 and 1000.");return;}
    const response=await fetch("/api/inventory/deposit",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({item_id:itemId,quantity})});
    const data=await response.json(); if(!response.ok||!data.ok){toast(data.error||"Could not add item.");return;}
    toast(data.message||"Item added."); await loadInventory(false); await loadWalletItems(); renderVirtualDeposit();
}


function selectedInventoryValue(){return selectedItemIds.reduce((sum,id)=>{const item=inventoryItems.find(x=>Number(x.id)===Number(id));return sum+(item?Number(item.total_value||(item.value*item.count)||0):0);},0);}
function updateWagerSummary(){const value=selectedInventoryValue(),count=selectedItemIds.length;const v=document.getElementById("createWagerValue"),c=document.getElementById("createWagerCount"),b=document.getElementById("createSubmitValue");if(v)v.textContent=money(value).replace("💎 ","");if(c)c.textContent=`${count} item${count===1?"":"s"}`;if(b)b.textContent=`💎 ${money(value).replace("💎 ","")}`;}
function toggleInventoryItem(id,targetName="create"){const target=targetName==="join"?pendingJoinItemIds:selectedItemIds;const idx=target.indexOf(Number(id));if(idx>=0)target.splice(idx,1);else target.push(Number(id));if(targetName==="join")pendingJoinItemIds=target;else selectedItemIds=target;renderCreateInventory();renderJoinInventory();updateWagerSummary();}
function renderCreateInventory(){
    const grid=document.getElementById("createInventoryGrid");
    if(!grid)return;
    const search=(document.getElementById("createItemSearch")?.value||"").trim().toLowerCase();
    const sort=document.getElementById("createItemSort")?.value||"high";
    let list=inventoryItems.filter(x=>{
        const name=String(x.name||"").toLowerCase();
        return !search || name.includes(search);
    });
    list.sort((a,b)=>sort==="low"?Number(a.value||0)-Number(b.value||0):Number(b.value||0)-Number(a.value||0));
    if(!list.length){
        grid.innerHTML='<div class="inventory-empty"><div style="font-size:16px;font-weight:900;color:#eef4ff;margin-bottom:6px">Your inventory is empty</div><div>Add gems or deposit pets before creating a match.</div></div>';
        updateWagerSummary();
        return;
    }
    grid.innerHTML=list.map(item=>`<button type="button" class="inventory-item ${selectedItemIds.includes(Number(item.id))?"selected":""}" onclick="toggleInventoryItem(${Number(item.id)})"><span class="inventory-item-count">x${Number(item.count||1)}</span><img src="${escapeHtml(item.icon||"")}" onerror="this.style.opacity=.2"><div class="inventory-item-name">${escapeHtml(item.name)}${item.variant?` · ${escapeHtml(item.variant)}`:""}</div><div class="inventory-item-value">💎 ${escapeHtml(formatCompact(item.value))}</div></button>`).join("");
    updateWagerSummary();
}

function formatCompact(value){const n=Number(value||0),units=[[1e12,"T"],[1e9,"B"],[1e6,"M"],[1e3,"K"]];for(const [t,u] of units){if(n>=t)return(n/t).toFixed(2).replace(/\.00$/,'').replace(/(\.\d)0$/,'$1')+u;}return n.toLocaleString("en-US",{maximumFractionDigits:2});}

function setMatchMode(mode) {
    selectedMatchMode = mode === "1v1v1" ? "1v1v1" : "1v1";
    const a=document.getElementById("mode1v1Btn"), b=document.getElementById("mode1v1v1Btn");
    if(a)a.classList.toggle("active", selectedMatchMode === "1v1");
    if(b)b.classList.toggle("active", selectedMatchMode === "1v1v1");
}

function setBestOf(value) {
    selectedBestOf = [1,2,3].includes(Number(value)) ? Number(value) : 1;
    [1,2,3].forEach(n=>{const b=document.getElementById(`bestOf${n}Btn`);if(b)b.classList.toggle("active",selectedBestOf===n);});
}

function toggleCreateSort(){
    const select=document.getElementById("createItemSort");
    if(select){select.value=select.value==="low"?"high":"low";}
    const label=document.getElementById("createSortLabel");
    if(label)label.textContent=(select?.value==="low")?"Lowest to Highest":"Highest to Lowest";
    renderCreateInventory();
}

function selectAllCreateItems(){
    const available=inventoryItems.filter(x=>Number(x.count||0)>0);
    if(!available.length){toast("Your inventory is empty.");return;}
    const allSelected=available.every(x=>selectedItemIds.includes(Number(x.id)));
    selectedItemIds=allSelected?[]:available.map(x=>Number(x.id));
    renderCreateInventory();
    updateWagerSummary();
}

function showCreateRules(){
    showGeneral("Rules",`<div style="padding:10px 4px;color:#aebbd0;line-height:1.7;font-size:12px"><b style="color:#fff">ColorDice PS99</b><br>Choose your virtual PS99 items, choose 1 or 2 colors, then create the match.<br><br>Your wager items stay inside the website and are transferred according to the match result. No real Roblox items are moved.</div>`);
}

async function openCreate() {

    try {

        const response = await fetch(
            "/api/me",
            {
                credentials: "same-origin"
            }
        );

        const data = await response.json();

        if (!data.logged_in) {

            toast("Login with Discord first.");

            setTimeout(() => {
                location.href = "/login";
            }, 500);

            return;
        }

        selectedColors = [];

        selectedColorCount = 1;

        document
            .querySelectorAll(".color-option")
            .forEach(button => {
                button.classList.remove("selected");
            });

        setColorCount(1);
        setMatchMode("1v1");
        setBestOf(1);
        selectedItemIds = [];
        await loadInventory(false);
        const balanceEl=document.getElementById("createBalanceValue");
        if(balanceEl) balanceEl.textContent=formatCompact(data.balance||0);
        updateWagerSummary();

        document
            .getElementById("createOverlay")
            .classList.add("open");

    } catch (error) {

        console.error(
            "LOGIN CHECK ERROR:",
            error
        );

        toast(
            "Could not check login status."
        );
    }
}


function closeModal(id) {

    document
        .getElementById(id)
        .classList.remove("open");
}


function outsideClose(event,id) {

    if(event.target.id === id) {

        closeModal(id);

    }
}


function setColorCount(count) {

    selectedColorCount = count;

    selectedColors = [];

    document
        .querySelectorAll(".color-option")
        .forEach(
            x =>
                x.classList.remove(
                    "selected"
                )
        );

    document
        .getElementById("oneColorBtn")
        .classList.toggle(
            "active",
            count === 1
        );

    document
        .getElementById("twoColorBtn")
        .classList.toggle(
            "active",
            count === 2
        );
}


function selectColor(color,button) {

    if(
        selectedColors.includes(color)
    ) {

        selectedColors =
            selectedColors.filter(
                x => x !== color
            );

        button.classList.remove(
            "selected"
        );

        return;
    }

    if(
        selectedColors.length >=
        selectedColorCount
    ) {

        toast(
            `You can choose ${selectedColorCount} color${selectedColorCount > 1 ? "s" : ""}.`
        );

        return;
    }

    selectedColors.push(color);

    button.classList.add(
        "selected"
    );
}


async function createMatch() {
    try {
        const meResponse=await fetch("/api/me",{credentials:"same-origin"}); const me=await meResponse.json();
        if(!me.logged_in){toast("Login with Discord first.");return;}
        if(!selectedItemIds.length){toast("Choose at least one item for your wager.");return;}
        if(selectedColors.length!==selectedColorCount){toast(`Choose exactly ${selectedColorCount} color${selectedColorCount>1?"s":""}.`);return;}
        const response=await fetch("/api/matches/create",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({item_ids:selectedItemIds,colors:selectedColors,mode:selectedMatchMode,best_of:selectedBestOf})});
        const data=await response.json(); if(!response.ok||!data.ok){toast(data.error||"Could not create match.");return;}
        closeModal("createOverlay");toast("Match created successfully.");await refreshMe();await loadMatches();
    } catch(error){console.error("CREATE MATCH ERROR",error);toast("Something went wrong while creating the match.");}
}


async function joinMatch(id) {
    const response=await fetch(`/api/matches/${id}`); const payload=await response.json();
    if(!response.ok||!payload.ok){toast("Could not load match.");return;}
    const match=payload.match; const count=(match.creator_colors||[]).length||1;
    pendingJoinMatchId=id; pendingJoinColors=[]; pendingJoinItemIds=[];
    const inv=await loadInventory(false); pendingJoinInventory=inventoryItems.slice();
    const taken=[...(match.creator_colors||[]),...(match.opponent_colors||[])];
    showGeneral("Join Match",`
      <div class="inventory-create-head"><div><div class="form-label">YOUR WAGER</div><div><span class="gem-icon">💎</span> <b id="joinWagerValue">0</b> · <span id="joinWagerCount">0 items</span></div></div><input id="joinItemSearch" class="inventory-search" placeholder="Search for an item..." oninput="renderJoinInventory()"></div>
      <div id="joinInventoryGrid" class="inventory-grid item-give-grid"></div>
      <div class="form-label" style="margin-top:15px">CHOOSE ${count} COLOR${count>1?"S":""}</div>
      <div class="color-grid" id="joinColorGrid">${Object.entries(COLORS).map(([name,hex])=>`<button class="color-option ${taken.includes(name)?"disabled":""}" data-join-color="${name}" style="background:${hex};${taken.includes(name)?"opacity:.3;cursor:not-allowed":""}" ${taken.includes(name)?"disabled":""} onclick="selectJoinColor('${name}',this)">${name}</button>`).join("")}</div>
      <button class="create-submit" onclick="submitJoinMatch()">JOIN MATCH</button>`);
    renderJoinInventory();
}

function renderJoinInventory(){
    const grid=document.getElementById("joinInventoryGrid"); if(!grid)return;
    const search=(document.getElementById("joinItemSearch")?.value||"").toLowerCase();
    const list=pendingJoinInventory.filter(x=>!search||String(x.name).toLowerCase().includes(search));
    if(!list.length){grid.innerHTML='<div class="inventory-empty">Your inventory is empty.</div>';return;}
    grid.innerHTML=list.map(item=>`<button type="button" class="inventory-item ${pendingJoinItemIds.includes(Number(item.id))?"selected":""}" onclick="toggleInventoryItem(${Number(item.id)},'join')"><span class="inventory-item-count">x${Number(item.count||1)}</span><img src="${escapeHtml(item.icon||"")}"><div class="inventory-item-name">${escapeHtml(item.name)}</div><div class="inventory-item-value">💎 ${escapeHtml(formatCompact(item.value))}</div></button>`).join("");
    const value=pendingJoinItemIds.reduce((sum,id)=>{const x=pendingJoinInventory.find(i=>Number(i.id)===Number(id));return sum+(x?Number(x.total_value||x.value*x.count):0)},0);
    const v=document.getElementById("joinWagerValue"),c=document.getElementById("joinWagerCount");if(v)v.textContent=formatCompact(value);if(c)c.textContent=`${pendingJoinItemIds.length} item${pendingJoinItemIds.length===1?"":"s"}`;
}

function selectJoinColor(color,button){
    fetch(`/api/matches/${pendingJoinMatchId}`).then(r=>r.json()).then(({match})=>{
        const count=(match?.creator_colors||[]).length||1;
        if(pendingJoinColors.includes(color)){pendingJoinColors=pendingJoinColors.filter(c=>c!==color);button.classList.remove("selected");}
        else if(pendingJoinColors.length<count){pendingJoinColors.push(color);button.classList.add("selected");}
        else toast(`Choose exactly ${count} color${count>1?"s":""}.`);
    });
}

async function submitJoinMatch(){
    const id=pendingJoinMatchId; if(!id)return;
    const mres=await fetch(`/api/matches/${id}`); const md=await mres.json(); const count=(md.match?.creator_colors||[]).length||1;
    if(pendingJoinColors.length!==count){toast(`Choose exactly ${count} color${count>1?"s":""}.`);return;}
    if(!pendingJoinItemIds.length){toast("Choose at least one item for your wager.");return;}
    const response=await fetch(`/api/matches/${id}/join`,{method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",body:JSON.stringify({colors:pendingJoinColors,item_ids:pendingJoinItemIds})});
    const data=await response.json(); if(!response.ok||!data.ok){toast(data.error||"Could not join.");return;}
    closeModal("generalOverlay");pendingJoinMatchId=null;toast(data.status==="waiting"?"Joined. Waiting for the third player...":"Match ready!");await openGame(id);await refreshMe();await loadMatches();
}


async function callBot(id) {

    try {

        const meResponse = await fetch(
            "/api/me",
            {
                credentials: "same-origin"
            }
        );

        const me =
            await meResponse.json();

        if (!me.logged_in) {

            toast(
                "Login with Discord first."
            );

            setTimeout(() => {
                location.href = "/login";
            }, 700);

            return;
        }

        const response = await fetch(
            `/api/matches/${id}/call-bot`,
            {
                method: "POST",

                credentials: "same-origin",

                headers: {
                    "Content-Type":
                        "application/json"
                }
            }
        );

        const data =
            await response.json();

        console.log(
            "CALL BOT:",
            data
        );

        if (
            !response.ok ||
            !data.ok
        ) {

            toast(
                data.error ||
                "Could not call bot."
            );

            return;
        }

        toast(
            "ColorDice Bot joined!"
        );

        await openGame(id);

        await refreshMe();

        await loadMatches();

    } catch (error) {

        console.error(
            "CALL BOT ERROR:",
            error
        );

        toast(
            "Could not call the bot."
        );
    }
}


async function cancelMatch(id) {

    const ok =
        confirm(
            "Cancel this match and refund your wager?"
        );

    if(!ok) return;

    const response =
        await fetch(
            `/api/matches/${id}/cancel`,
            {
                method: "POST"
            }
        );

    const data =
        await response.json();

    if(
        !response.ok
        || !data.ok
    ) {

        toast(
            data.error ||
            "Could not cancel."
        );

        return;
    }

    toast(
        "Match cancelled. Wager refunded."
    );

    await refreshMe();

    await loadMatches();
}


async function openGame(id) {
    currentMatchId=id;
    const response=await fetch(`/api/matches/${id}`,{credentials:"same-origin",cache:"no-store"}); const data=await response.json();
    if(!response.ok||!data.ok){toast("Match not found.");return;}
    document.getElementById("gameOverlay").classList.add("open");
    renderGame(data.match);
    if(data.match.status === "finished" && (data.match.roll_history||[]).length && !activeGameAnimations.has(Number(id))){
        activeGameAnimations.add(Number(id));
        try{await playMatchAnimation(data.match);}finally{activeGameAnimations.delete(Number(id));}
    }
}

async function startMatch(id){
    const response=await fetch(`/api/matches/${id}/start`,{method:"POST",credentials:"same-origin"}); const data=await response.json();
    if(!response.ok||!data.ok){toast(data.error||"Could not start match.");return;}
    toast("Game started!"); renderGame(data.match);
    if(!activeGameAnimations.has(Number(id))){activeGameAnimations.add(Number(id));try{await playMatchAnimation(data.match);}finally{activeGameAnimations.delete(Number(id));}}
    await refreshMe(); await loadMatches();
}


function renderColors(colors) {

    if (!colors || !colors.length) {

        return `
            <span
                class="color-tag"
                style="background:#334155"
            >
                None
            </span>
        `;
    }

    return `
        <div class="color-tags">

            ${colors.map(
                color => `
                    <span
                        class="color-tag"
                        style="background:${COLORS[color] || '#475569'}"
                    >
                        ${escapeHtml(color)}
                    </span>
                `
            ).join("")}

        </div>
    `;
}


function renderGame(match) {

    const c = match.creator;
    const o = match.opponent;
    const canStart = match.status === "ready";

    const creatorAvatar =
        c?.avatar ||
        "https://cdn.discordapp.com/embed/avatars/0.png";

    const opponentAvatar =
        o?.avatar ||
        "https://cdn.discordapp.com/embed/avatars/1.png";

    document
        .getElementById("gameContent")
        .innerHTML = `

        <div class="game-top ${match.match_mode === '1v1v1' ? 'game-top-three' : ''}">

            <div
                class="player game-player-card"
                id="gameCreatorCard"
                data-player-id="${Number(c?.id || 0)}"
                onclick="openUserProfile(${Number(c?.id || 0)})"
            >

                <img
                    class="big-avatar"
                    src="${escapeHtml(creatorAvatar)}"
                >

                <div class="player-name">
                    ${escapeHtml(
                        c?.display_name ||
                        "Player 1"
                    )}
                </div>

                ${renderColors(
                    match.creator_colors
                )}

            </div>


            <div class="roll-center">

                <div
                    id="gameRollTitle"
                    class="roll-title"
                >
                    GET READY
                </div>

                <div
                    id="gameRolls"
                    class="rolls"
                >
                    ${blankDiceHtml()}
                </div>

                <div
                    id="gameStatus"
                    class="game-status"
                >
                    <span class="game-countdown">READY</span>
                </div>

            </div>


            <div
                class="player game-player-card"
                id="gameOpponentCard"
                data-player-id="${Number(o?.id || -1)}"
                ${
                    o
                        ? `onclick="openUserProfile(${Number(o.id)})"`
                        : ""
                }
            >

                <img
                    class="big-avatar"
                    src="${escapeHtml(opponentAvatar)}"
                >

                <div class="player-name">
                    ${escapeHtml(
                        o?.display_name ||
                        "ColorDice Bot"
                    )}
                </div>

                ${
                    o
                    ? renderColors(
                        match.opponent_colors
                      )
                    : `
                        <span
                            class="color-tag"
                            style="background:#334155"
                        >
                            Waiting
                        </span>
                    `
                }

            </div>

            ${match.match_mode === "1v1v1" ? `
            <div class="player game-player-card" id="gameThirdCard" data-player-id="${Number(match.third_player?.id || -2)}" ${match.third_player?.id > 0 ? `onclick="openUserProfile(${Number(match.third_player.id)})"` : ""}>
                <img class="big-avatar" src="${escapeHtml(match.third_player?.avatar || "https://cdn.discordapp.com/embed/avatars/2.png")}">
                <div class="player-name">${escapeHtml(match.third_player?.display_name || "Player 3")}</div>
                ${renderColors(match.third_colors || [])}
            </div>
            ` : ""}

        </div>

        ${Number(match.best_of || 1) > 1 ? `
          <div class="series-scoreboard" id="seriesScoreboard" style="margin:14px auto 18px;padding:12px 18px;max-width:520px;text-align:center;background:#0d1525;border:1px solid #334155;border-radius:12px;color:#e2e8f0;">
            <div style="font-size:12px;letter-spacing:1.5px;color:#94a3b8;font-weight:800;">FIRST TO ${Number(match.best_of)}</div>
            <div id="seriesScoreValue" style="font-size:22px;font-weight:900;margin-top:5px;">0 : 0</div>
            <div id="seriesScoreHint" style="font-size:12px;color:#fbbf24;margin-top:4px;">Match points</div>
          </div>
        ` : ""}

        <div class="history">

            <div
                id="historyTitle"
                class="history-title"
            >
                ROLL HISTORY · 0 ROLLS
            </div>

            <div id="historyRows"></div>

        </div>


        <div class="modal-bottom">
            <div class="wager-block">
                <label>${escapeHtml(c?.display_name || "Player 1")} · WAGER</label>
                <div class="wager-item-preview">${matchItemsHtml(match.creator_items, match.creator_item_value)}</div>
            </div>
            <div class="wager-block">
                <label>${escapeHtml(o?.display_name || "ColorDice Bot")} · WAGER</label>
                <div class="wager-item-preview">${matchItemsHtml(match.opponent_items, match.opponent_item_value)}</div>
            </div>
            ${match.match_mode === "1v1v1" ? `<div class="wager-block"><label>${escapeHtml(match.third_player?.display_name || "Player 3")} · WAGER</label><div class="wager-item-preview">${matchItemsHtml(match.third_items, match.third_item_value)}</div></div>` : ""}
        </div>
        ${canStart ? `<div style="display:flex;justify-content:center;padding:14px 0 4px"><button class="primary" style="min-width:190px" onclick="startMatch(${Number(match.id)})">▶ START GAME</button></div>` : ""}
    `;
}


function blankDiceHtml() {

    return `
        <div class="roll-box" data-die="0" style="background:#28364d"></div>
        <div class="roll-box" data-die="1" style="background:#28364d"></div>
        <div class="roll-box" data-die="2" style="background:#28364d"></div>
        <div class="roll-box" data-die="3" style="background:#28364d"></div>
    `;
}


function blankDice() {

    const container =
        document.getElementById(
            "gameRolls"
        );

    if (!container) return;

    container.innerHTML =
        blankDiceHtml();
}


function sleep(ms) {

    return new Promise(
        resolve => setTimeout(
            resolve,
            ms
        )
    );
}


function setGameStatus(html) {

    const el =
        document.getElementById(
            "gameStatus"
        );

    if (el) {
        el.innerHTML = html;
    }
}


function setRollTitle(text) {

    const el =
        document.getElementById(
            "gameRollTitle"
        );

    if (el) {
        el.textContent = text;
    }
}


function scoreColors(rolls, selectedColors) {

    return rolls.filter(
        color =>
            selectedColors.includes(color)
    ).length;
}


async function countdown() {

    setRollTitle(
        "GET READY"
    );

    for (
        let number = 3;
        number >= 1;
        number--
    ) {

        setGameStatus(`
            <span class="game-countdown">
                ${number}
            </span>
        `);

        await sleep(1000);
    }

    setGameStatus(`
        <span
            style="font-size:12px;letter-spacing:3px"
        >
            ROLLING...
        </span>
    `);
}


async function animateRoll(rolls) {

    const container =
        document.getElementById(
            "gameRolls"
        );

    if (!container) return;

    const dice = [
        ...container.querySelectorAll(
            ".roll-box"
        )
    ];

    dice.forEach(
        die => {
            die.classList.add(
                "rolling"
            );
        }
    );

    setRollTitle(
        "ROLLING..."
    );

    await sleep(700);

    dice.forEach(
        die => {
            die.classList.remove(
                "rolling"
            );
        }
    );

    for (
        let i = 0;
        i < Math.min(4, rolls.length);
        i++
    ) {

        const die = dice[i];
        const color = rolls[i];

        if (!die) continue;

        die.style.background =
            COLORS[color] ||
            "#475569";

        die.classList.add(
            "reveal"
        );

        await sleep(240);
    }
}


function addHistoryRound(
    roundNumber,
    rolls,
    creatorScore,
    opponentScore
) {

    const historyRows =
        document.getElementById(
            "historyRows"
        );

    const historyTitle =
        document.getElementById(
            "historyTitle"
        );

    if (!historyRows) return;

    const dots =
        rolls.map(
            color => `
                <div
                    class="color-mini"
                    style="background:${COLORS[color] || '#475569'}"
                    title="${escapeHtml(color)}"
                ></div>
            `
        ).join("");

    const tied =
        creatorScore ===
        opponentScore;

    const row =
        document.createElement(
            "div"
        );

    row.className =
        "history-row new-round";

    row.innerHTML = `

        <div class="history-num">
            #${roundNumber}
        </div>

        ${dots}

        <div
            class="history-score"
            style="${
                tied
                    ? "color:#f5c542"
                    : ""
            }"
        >
            ${creatorScore} : ${opponentScore}
        </div>
    `;

    historyRows.appendChild(
        row
    );

    historyTitle.textContent =
        `ROLL HISTORY · ${roundNumber} ${
            roundNumber === 1
                ? "ROLL"
                : "ROLLS"
        }`;
}


async function playMatchAnimation(match) {

    const history =
        match.roll_history &&
        match.roll_history.length
            ? match.roll_history
            : (
                match.rolls &&
                match.rolls.length
                    ? [match.rolls]
                    : []
            );

    if (!history.length) {

        setRollTitle(
            "WAITING"
        );

        setGameStatus(
            "Waiting for the match to start..."
        );

        return;
    }

    if (match.match_mode === "1v1v1") {
        const players = [
            {id:Number(match.creator?.id), name:match.creator?.display_name || "Player 1", colors:match.creator_colors || []},
            {id:Number(match.opponent?.id || -1), name:match.opponent?.display_name || "Bot 1", colors:match.opponent_colors || []},
            {id:Number(match.third_player?.id || -2), name:match.third_player?.display_name || "Bot 2", colors:match.third_colors || []}
        ];
        let alive = [0,1,2];
        const historyRows = document.getElementById("historyRows");
        if (historyRows) historyRows.innerHTML = "";
        blankDice();
        await countdown();
        for (let i=0; i<history.length; i++) {
            if (i > 0) { blankDice(); setRollTitle(`REMATCH #${i+1}`); setGameStatus('<span class="reroll-text">Tied players rematch...</span>'); await sleep(500); }
            const rolls = history[i];
            setRollTitle(`ROUND #${i+1}`);
            await animateRoll(rolls);
            const scores = players.map(p => scoreColors(rolls,p.colors));
            const high = Math.max(...alive.map(idx => scores[idx]));
            const survivors = alive.filter(idx => scores[idx] === high);
            players.forEach((p,idx) => {
                const card = document.querySelector(`.game-player-card[data-player-id="${p.id}"]`);
                if (card && !survivors.includes(idx)) card.style.opacity = ".35";
            });
            const scoreLine = alive.map(idx => `${players[idx].name}: ${scores[idx]}`).join(" · ");
            if (historyRows) {
                const diceHtml = rolls.map(color => `<span class="color-mini" style="background:${COLORS[color] || '#475569'}" title="${escapeHtml(color)}"></span>`).join("");
                historyRows.insertAdjacentHTML("beforeend", `<div class="history-row new-round three-player-history"><div class="history-num">#${i+1}</div><div class="history-dice">${diceHtml}</div><div class="history-score">${alive.map(idx => `${escapeHtml(players[idx].name)}: ${scores[idx]}`).join(" · ")}</div></div>`);
            }
            const historyTitle = document.getElementById("historyTitle");
            if (historyTitle) historyTitle.textContent = `ROLL HISTORY · ${i+1} ${i === 0 ? "ROLL" : "ROLLS"}`;
            alive = survivors;
            if (alive.length === 1 || i === history.length-1) {
                const winner = players[alive[0]];
                const card = document.querySelector(`.game-player-card[data-player-id="${winner.id}"]`);
                if (card) { card.style.opacity = "1"; card.classList.add("winner-card"); card.insertAdjacentHTML("afterbegin", '<span class="winner-badge">WINNER</span>'); }
                setRollTitle("FINAL RESULT");
                setGameStatus(`<span class="winner">${escapeHtml(winner.name)} wins</span>`);
                return;
            }
        }
        return;
    }

    const creatorColors =
        match.creator_colors || [];

    const opponentColors =
        match.opponent_colors || [];

    const historyRows =
        document.getElementById(
            "historyRows"
        );

    if (historyRows) {
        historyRows.innerHTML = "";
    }

    blankDice();

    // Exactly 3 seconds before the first roll.
    await countdown();

    // First-to series: replay every round and update the match-point counter.
    if (Number(match.best_of || 1) > 1 && match.series_wins && match.series_wins.results) {
        const series = match.series_wins;
        const scoreMap = {};
        const creatorId = String(match.creator?.id);
        const opponentId = String(match.opponent?.id || -1);
        scoreMap[creatorId] = 0; scoreMap[opponentId] = 0;
        let resultIndex = 0;
        for (let i = 0; i < history.length; i++) {
            blankDice();
            setRollTitle(`MATCH POINT ${resultIndex + 1} · ROLL ${i + 1}`);
            await animateRoll(history[i]);
            addHistoryRound(i + 1, history[i], scoreColors(history[i], creatorColors), scoreColors(history[i], opponentColors));
            while (resultIndex < series.results.length && Number(series.results[resultIndex].roll_index) === i) {
                const point = series.results[resultIndex];
                const id = String(point.winner_id);
                scoreMap[id] = (scoreMap[id] || 0) + 1;
                resultIndex++;
                const scoreEl = document.getElementById("seriesScoreValue");
                if (scoreEl) scoreEl.textContent = `${scoreMap[creatorId] || 0} : ${scoreMap[opponentId] || 0}`;
                const hint = document.getElementById("seriesScoreHint");
                if (hint) hint.textContent = `First to ${Number(match.best_of)} · Point ${resultIndex}`;
                setGameStatus(`<span class="reroll-text">Match point ${resultIndex} recorded</span>`);
                await sleep(650);
            }
        }
        const finalWinnerId = Number(match.winner_id);
        const finalWinner = finalWinnerId === Number(match.creator?.id) ? match.creator : match.opponent;
        const winnerCard = document.querySelector(`.game-player-card[data-player-id="${finalWinnerId}"]`);
        if (winnerCard) { winnerCard.classList.add("winner-card"); if (!winnerCard.querySelector(".winner-badge")) winnerCard.insertAdjacentHTML("afterbegin", '<span class="winner-badge">WINNER</span>'); }
        setRollTitle("FINAL RESULT");
        setGameStatus(`<span class="winner">${escapeHtml(finalWinner?.display_name || "Winner")} wins · First to ${Number(match.best_of)}</span>`);
        return;
    }

    for (
        let i = 0;
        i < history.length;
        i++
    ) {

        if (i > 0) {

            blankDice();

            setRollTitle(
                `REROLL #${i + 1}`
            );

            setGameStatus(`
                <span class="reroll-text">
                    Rerolling...
                </span>
            `);

            await sleep(500);
        }

        const rolls = history[i];

        blankDice();

        setRollTitle(
            `ROLL #${i + 1}`
        );

        await animateRoll(
            rolls
        );

        const creatorScore =
            scoreColors(
                rolls,
                creatorColors
            );

        const opponentScore =
            scoreColors(
                rolls,
                opponentColors
            );

        addHistoryRound(
            i + 1,
            rolls,
            creatorScore,
            opponentScore
        );

        if (
            creatorScore ===
            opponentScore
        ) {

            setRollTitle(
                "TIE"
            );

            setGameStatus(`
                <span class="reroll-text">
                    Rerolling...
                </span>
            `);

            await sleep(500);

            continue;
        }

        const creatorWon =
            creatorScore >
            opponentScore;

        const winnerName =
            creatorWon
                ? (
                    match.creator?.display_name ||
                    "Player 1"
                  )
                : (
                    match.opponent?.display_name ||
                    "ColorDice Bot"
                  );

        setRollTitle(
            "FINAL RESULT"
        );

        const winnerId =
            creatorWon
                ? Number(match.creator?.id)
                : Number(match.opponent?.id || -1);

        const winnerCard =
            document.querySelector(
                `.game-player-card[data-player-id="${winnerId}"]`
            );

        if (winnerCard) {

            winnerCard.classList.add(
                "winner-card"
            );

            if (
                !winnerCard.querySelector(
                    ".winner-badge"
                )
            ) {

                winnerCard.insertAdjacentHTML(
                    "afterbegin",
                    `<span class="winner-badge">WINNER</span>`
                );
            }
        }

        setGameStatus(`
            <span
                class="winner"
                style="margin-top:0"
            >
                ${escapeHtml(
                    winnerName
                )} wins
            </span>
        `);

        return;
    }
}


function playerColorsHtml(colors) {

    return (colors || []).map(
        color => `
            <span
                class="tiny-color"
                style="background:${COLORS[color] || "#475569"}"
                title="${escapeHtml(color)}"
            ></span>
        `
    ).join("");
}


function miniDiceHtml() {

    return `
        <div class="mini-rolls">
            <div class="mini-die" data-mini-die="0"></div>
            <div class="mini-die" data-mini-die="1"></div>
            <div class="mini-die" data-mini-die="2"></div>
            <div class="mini-die" data-mini-die="3"></div>
        </div>
    `;
}


function getAnimationTimeline(match) {

    const history =
        match.roll_history || [];

    let cursor = 3000;
    const rounds = [];

    for (let i = 0; i < history.length; i++) {

        const rollingStart = cursor;
        const rollingEnd = cursor + 700;
        const revealStart = rollingEnd;
        const revealEnd = revealStart + 4 * 240;

        const creatorScore =
            scoreColors(
                history[i],
                match.creator_colors || []
            );

        const opponentScore =
            scoreColors(
                history[i],
                match.opponent_colors || []
            );

        const tie =
            creatorScore === opponentScore;

        const end =
            revealEnd + (tie ? 500 : 0);

        rounds.push({
            index: i,
            rollingStart,
            rollingEnd,
            revealStart,
            revealEnd,
            end,
            tie
        });

        cursor = end;
    }

    return {
        rounds,
        total: cursor
    };
}


function getLiveAnimationState(match) {

    const history =
        match.roll_history || [];

    if (
        !history.length ||
        !match.animation_started_at
    ) {
        return {
            phase: "finished",
            round: history.length - 1,
            revealed: 4,
            elapsed: Infinity
        };
    }

    const elapsed =
        Date.now() -
        Number(match.animation_started_at);

    const timeline =
        getAnimationTimeline(match);

    if (elapsed < 3000) {

        return {
            phase: "countdown",
            countdown:
                Math.max(
                    1,
                    3 - Math.floor(elapsed / 1000)
                ),
            round: -1,
            revealed: 0,
            elapsed
        };
    }

    for (const r of timeline.rounds) {

        if (elapsed < r.end) {

            if (
                elapsed >= r.revealEnd &&
                r.tie
            ) {
                return {
                    phase: "reroll",
                    round: r.index,
                    revealed: 4,
                    elapsed
                };
            }

            if (elapsed < r.rollingEnd) {

                return {
                    phase: "rolling",
                    round: r.index,
                    revealed: 0,
                    elapsed
                };
            }

            const revealed =
                Math.min(
                    4,
                    Math.floor(
                        (
                            elapsed -
                            r.revealStart
                        ) / 240
                    ) + 1
                );

            return {
                phase: "reveal",
                round: r.index,
                revealed,
                elapsed
            };
        }
    }

    return {
        phase: "finished",
        round: history.length - 1,
        revealed: 4,
        elapsed
    };
}


function updateLiveMatchCards() {

    document
        .querySelectorAll("[data-live-match]")
        .forEach(card => {

            const id =
                Number(
                    card.dataset.liveMatch
                );

            const match =
                liveMatches.find(
                    x => Number(x.id) === id
                );

            if (!match) return;

            const state =
                getLiveAnimationState(match);

            const title =
                card.querySelector(
                    ".match-live-title"
                );

            const dice =
                [
                    ...card.querySelectorAll(
                        ".mini-die"
                    )
                ];

            const history =
                match.roll_history || [];

            dice.forEach(
                (die, i) => {

                    die.classList.remove(
                        "mini-rolling"
                    );

                    if (
                        state.round >= 0 &&
                        history[state.round] &&
                        i < state.revealed
                    ) {

                        die.style.background =
                            COLORS[
                                history[
                                    state.round
                                ][i]
                            ] || "#475569";

                    } else {

                        die.style.background =
                            "#2a394e";
                    }
                }
            );

            if (
                state.phase === "countdown"
            ) {

                title.innerHTML = `
                    <span class="live-countdown">
                        ${state.countdown}
                    </span>
                `;

            } else if (
                state.phase === "rolling"
            ) {

                title.textContent =
                    "ROLLING...";

                dice.forEach(
                    die =>
                        die.classList.add(
                            "mini-rolling"
                        )
                );

            } else if (
                state.phase === "reroll"
            ) {

                title.innerHTML = `
                    <span class="live-reroll">
                        REROLLING...
                    </span>
                `;

            } else if (
                state.phase === "reveal"
            ) {

                title.textContent =
                    `ROLL #${state.round + 1}`;

            } else {

                title.textContent =
                    match.winner_id
                        ? "FINAL RESULT"
                        : "WAITING";
            }

            const winnerDone =
                state.phase === "finished" &&
                match.winner_id;

            card
                .querySelectorAll(".player-mini")
                .forEach(
                    player => {

                        const playerId =
                            Number(
                                player.dataset.playerId
                            );

                        const isWinner =
                            winnerDone &&
                            playerId ===
                                Number(
                                    match.winner_id
                                );

                        player.classList.toggle(
                            "winner-card",
                            isWinner
                        );

                        const badge =
                            player.querySelector(
                                ".winner-badge"
                            );

                        if (
                            isWinner &&
                            !badge
                        ) {
                            player.insertAdjacentHTML(
                                "afterbegin",
                                `<span class="winner-badge">WINNER</span>`
                            );
                        } else if (
                            !isWinner &&
                            badge
                        ) {
                            badge.remove();
                        }
                    }
                );
        });
}


function matchItemsHtml(items, value){
    const list=(items||[]).filter(x=>!x.virtual);
    if(!list.length){ return `<div class="match-wager-items"><span style="color:#71839d;font-size:10px;font-weight:900">${items?.some(x=>x.virtual)?"BOT POOL":"NO ITEMS"}</span></div><div class="match-wager-value">💎 ${escapeHtml(formatCompact(value||0))}</div>`; }
    const shown=list.slice(0,5);
    return `<div class="match-wager-items">${shown.map(x=>`<img src="${escapeHtml(x.icon||"")}" title="${escapeHtml(x.name||"")}" onerror="this.style.opacity=.2">`).join("")}${list.length>5?`<span class="more">+${list.length-5}</span>`:""}</div><div class="match-wager-value">💎 ${escapeHtml(formatCompact(value||0))}<small>${list.length} item${list.length===1?"":"s"}</small></div>`;
}

async function loadMatches() {

    try {

        const [
            matchesResponse,
            meResponse
        ] = await Promise.all([
            fetch("/api/matches", {
                credentials: "same-origin"
            }),
            fetch("/api/me", {
                credentials: "same-origin"
            })
        ]);

        const matches =
            await matchesResponse.json();

        const me =
            await meResponse.json();

        liveMatches =
            matches;

        const currentUserId =
            me.logged_in
                ? Number(me.id)
                : null;

        for (const m of matches) {
            const oldStatus = previousMatchStatuses[m.id];
            const participantIds = [m.creator?.id, m.opponent?.id, m.third_player?.id].map(Number);
            if (oldStatus === "waiting" && ["ready", "finished"].includes(m.status) && currentUserId !== null && participantIds.includes(currentUserId)) {
                openGame(m.id);
            }
            previousMatchStatuses[m.id] = m.status;
        }

        const filter =
            document.getElementById("filter").value;

        const sort =
            document.getElementById("sort").value;

        let filtered =
            [...matches];

        if (filter === "waiting") {

            filtered =
                matches.filter(
                    x => x.status === "waiting"
                );

        } else if (filter === "finished") {

            filtered =
                matches.filter(
                    x => x.status === "finished"
                );
        }

        filtered.sort(
            (a, b) =>
                sort === "high"
                    ? Number(b.bet) -
                      Number(a.bet)
                    : Number(a.bet) -
                      Number(b.bet)
        );

        const active =
            matches.filter(
                x =>
                    x.status === "waiting" ||
                    x.status === "ready" ||
                    (
                        x.status === "finished" &&
                        getLiveAnimationState(x).phase !== "finished"
                    )
            );

        const totalValue =
            active.reduce(
                (sum, x) =>
                    sum + Number(x.total_item_value || x.bet || 0),
                0
            );

        document.getElementById("activeCount").textContent =
            active.length;

        document.getElementById("totalValue").textContent =
            money(totalValue);

        document.getElementById("totalGames").textContent =
            matches.length;

        const list =
            document.getElementById("matchList");

        // Keep the existing match DOM/design intact while the live
        // animation timer updates the dice every frame. Rebuilding
        // the whole list on every poll would reset CSS animations.
        const renderSignature =
            JSON.stringify({
                filter,
                sort,
                matches: filtered.map(m => ({
                    id: m.id,
                    status: m.status,
                    creator: m.creator?.id || null,
                    opponent: m.opponent?.id || null,
                    third_player: m.third_player?.id || null,
                    match_mode: m.match_mode || "1v1",
                    opponent_is_bot: !!m.opponent_is_bot,
                    winner_id: m.winner_id || null,
                    bet: m.bet,
                    created_at: m.created_at,
                    finished_at: m.finished_at,
                    creator_colors: m.creator_colors || [],
                    opponent_colors: m.opponent_colors || [],
                    third_colors: m.third_colors || [],
                    creator_items: (m.creator_items || []).map(x=>x.id),
                    opponent_items: (m.opponent_items || []).map(x=>x.id),
                    third_items: (m.third_items || []).map(x=>x.id),
                    total_item_value: m.total_item_value || 0
                }))
            });

        if (renderSignature === lastMatchListSignature) {
            updateLiveMatchCards();
            return;
        }

        lastMatchListSignature = renderSignature;

        if (!filtered.length) {

            list.innerHTML = `
                <div class="empty">
                    No matches found.<br>
                    Create the first Color Dice duel.
                </div>
            `;

            return;
        }

        list.innerHTML =
            filtered.map(
                match => {

                    const c =
                        match.creator;

                    const o =
                        match.opponent;

                    const cAvatar =
                        c?.avatar ||
                        "https://cdn.discordapp.com/embed/avatars/0.png";

                    const oAvatar =
                        o?.avatar ||
                        "https://cdn.discordapp.com/embed/avatars/1.png";

                    const canJoin =
                        match.status === "waiting";

                    const isCreator =
                        currentUserId !== null &&
                        c &&
                        Number(c.id) ===
                            currentUserId;

                    const cId =
                        Number(c?.id || 0);

                    const oId =
                        Number(o?.id || -1);

                    return `
                        <div
                            class="match"
                            data-live-match="${match.id}"
                        >

                            <div class="players">

                                <div
                                    class="player-mini"
                                    data-player-id="${cId}"
                                    onclick="openUserProfile(${cId})"
                                >

                                    <div class="player-mini-colors">
                                        ${playerColorsHtml(
                                            match.creator_colors
                                        )}
                                    </div>

                                    <div class="player-mini-line">

                                        <img
                                            class="avatar"
                                            src="${escapeHtml(cAvatar)}"
                                        >

                                        <span class="player-mini-name">
                                            ${escapeHtml(
                                                c?.display_name ||
                                                "Player"
                                            )}
                                        </span>

                                    </div>

                                </div>

                                <div class="vs">
                                    VS
                                </div>

                                <div
                                    class="player-mini"
                                    data-player-id="${oId}"
                                    ${
                                        o
                                            ? `onclick="openUserProfile(${oId})"`
                                            : ""
                                    }
                                >

                                    <div class="player-mini-colors">
                                        ${
                                            o
                                            ? playerColorsHtml(
                                                match.opponent_colors
                                              )
                                            : ""
                                        }
                                    </div>

                                    <div class="player-mini-line">

                                        ${
                                            o
                                            ? `
                                                <img
                                                    class="avatar"
                                                    src="${escapeHtml(oAvatar)}"
                                                >
                                            `
                                            : `
                                                <div class="waiting-avatar">
                                                    ?
                                                </div>
                                            `
                                        }

                                        <span class="player-mini-name">
                                            ${escapeHtml(
                                                o?.display_name ||
                                                "Waiting"
                                            )}
                                        </span>

                                    </div>

                                </div>

                                ${match.match_mode === "1v1v1" ? `
                                    <div class="vs">VS</div>
                                    <div class="player-mini" data-player-id="${Number(match.third_player?.id || -2)}" ${match.third_player?.id > 0 ? `onclick="openUserProfile(${Number(match.third_player.id)})"` : ""}>
                                        <div class="player-mini-colors">${playerColorsHtml(match.third_colors || [])}</div>
                                        <div class="player-mini-line">
                                            ${match.third_player ? `<img class="avatar" src="${escapeHtml(match.third_player.avatar || "https://cdn.discordapp.com/embed/avatars/2.png")}">` : `<div class="waiting-avatar">?</div>`}
                                            <span class="player-mini-name">${escapeHtml(match.third_player?.display_name || (match.third_is_bot ? "ColorDice Bot 2" : "Waiting"))}</span>
                                        </div>
                                    </div>
                                ` : ""}

                            </div>

                            <div class="match-live match-live-items">
                                <div class="match-item-battle">
                                    <div class="match-item-side">${matchItemsHtml(match.creator_items, match.creator_item_value)}</div>
                                    <div class="match-item-vs">VS</div>
                                    <div class="match-item-side">${matchItemsHtml(match.opponent_items, match.opponent_item_value)}</div>
                                </div>
                                ${match.match_mode === "1v1v1" ? `<div class="match-item-battle" style="margin-top:4px"><div class="match-item-vs">VS</div>${matchItemsHtml(match.third_items, match.third_item_value)}</div>` : ""}
                                <div class="match-live-title">${match.status === "waiting" ? "WAITING" : match.status === "ready" ? "READY" : "RESULT"}</div>
                            </div>

                            <div class="wager">
                                💎 ${formatCompact(match.total_item_value || match.bet)}
                                <small>
                                    ${
                                        o
                                        ? (match.match_mode === "1v1v1" ? "3 PLAYER POT" : "2 PLAYER POT")
                                        : (match.match_mode === "1v1v1" ? "WAITING FOR 2 PLAYERS" : "WAITING FOR PLAYER")
                                    }
                                </small>
                            </div>

                            <div class="status">

                                ${
                                    match.status === "finished" &&
                                    getLiveAnimationState(match).phase === "finished"
                                    ? `
                                        <div class="score">
                                            ${
                                                match.winner_id === c?.id
                                                ? "1 - 0"
                                                : "0 - 1"
                                            }
                                        </div>
                                    `
                                    : `
                                        <div class="score">
                                            ${
                                                match.status === "waiting"
                                                ? "WAITING"
                                                : "LIVE"
                                            }
                                        </div>
                                    `
                                }

                            </div>

                            <div class="actions">

                                ${
                                    canJoin && !isCreator
                                    ? `
                                        <button
                                            class="join"
                                            onclick="joinMatch(${match.id})"
                                        >
                                            Join
                                        </button>
                                    `
                                    : ""
                                }

                                ${
                                    canJoin && isCreator
                                    ? `
                                        <button
                                            class="callbot"
                                            onclick="callBot(${match.id})"
                                        >
                                            Call Bot
                                        </button>

                                        <button
                                            class="cancel"
                                            onclick="cancelMatch(${match.id})"
                                        >
                                            Cancel
                                        </button>
                                    `
                                    : ""
                                }

                                <button class="view" onclick="openGame(${match.id})">View</button>

                            </div>

                        </div>
                    `;
                }
            ).join("");

        updateLiveMatchCards();

    } catch (error) {

        console.error(
            "[LOAD MATCHES ERROR]",
            error
        );
    }
}


if (!matchAnimationTimer) {
    matchAnimationTimer = setInterval(
        updateLiveMatchCards,
        50
    );
}


async function refreshMe() {

    const response =
        await fetch(
            "/api/me"
        );

    const data =
        await response.json();

    if(!data.logged_in)
        return;

    const balance =
        document.getElementById(
            "topBalance"
        );

    if(balance) {

        balance.textContent =
            Number(
                data.balance
            ).toLocaleString(
                "en-US",
                {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2
                }
            );
    }
}


async function loadChat() {

    try {

        const response =
            await fetch("/api/chat", {
                credentials: "same-origin"
            });

        const messages =
            await response.json();

        const list =
            document.getElementById("chatList");

        if (!list) return;

        list.innerHTML =
            messages.map(
                m => `
                <div
                    class="chat-msg"
                    onclick="openUserProfile(${Number(m.user_id || 0)})"
                >

                    <div class="chat-avatar-wrap">

                        <img
                            src="${escapeHtml(
                                m.avatar ||
                                "https://cdn.discordapp.com/embed/avatars/0.png"
                            )}"
                        >

                        <span class="chat-level">
                            ${escapeHtml(String(m.level || 1))}
                        </span>

                    </div>

                    <div class="chat-body">

                        <div class="chat-meta">

                            <span class="chat-name">
                                ${escapeHtml(m.display_name)}
                            </span>

                            <span class="chat-verified">
                                ✓
                            </span>

                            <span class="chat-time">
                                ${formatChatTime(m.created_at)}
                            </span>

                            <span class="chat-reply">
                                ↪
                            </span>

                        </div>

                        <div class="chat-text">
                            ${escapeHtml(m.message)}
                        </div>

                    </div>

                </div>
                `
            ).join("");

        list.scrollTop =
            list.scrollHeight;

    } catch (error) {

        console.error(
            "[CHAT ERROR]",
            error
        );

    }
}


function formatChatTime(timestamp) {

    if (!timestamp) return "";

    const d =
        new Date(Number(timestamp) * 1000);

    return d.toLocaleTimeString(
        "ko-KR",
        {
            hour: "2-digit",
            minute: "2-digit",
            hour12: true
        }
    );
}


async function openUserProfile(userId) {

    if (!userId) return;

    try {

        const response =
            await fetch(
                `/api/users/${userId}`
            );

        const data =
            await response.json();

        if (!response.ok || !data.ok) {

            toast("Profile not found.");

            return;
        }

        const u = data.user;
        const history = data.history || [];

        showGeneral(
            "Player Profile",
            `
            <div class="profile-card">

                <div class="profile-hero">

                    <div class="profile-avatar-wrap">

                        <img
                            class="profile-avatar"
                            src="${escapeHtml(u.avatar)}"
                        >

                        <span class="profile-level">
                            LV.${u.level}
                        </span>

                    </div>

                    <div class="profile-main">

                        <div class="profile-name">
                            ${escapeHtml(u.display_name)}
                            <span class="chat-verified">✓</span>
                        </div>

                        <div class="profile-username">
                            @${escapeHtml(u.username)}
                        </div>

                    </div>

                </div>

                <div class="profile-action-row">
                    <button class="primary" onclick="sendTipTo(${Number(u.id)}, '${escapeHtml(u.display_name).replaceAll("'", "\\'")}')">💎 Tip</button>
                    <button class="secondary" onclick="openGiveItem(${Number(u.id)}, '${escapeHtml(u.display_name).replaceAll("'", "\\'")}')">🎁 Give Item</button>
                </div>

                <div class="profile-stats">

                    <div>
                        <b>LV.${u.level}</b>
                        <span>Level</span>
                    </div>

                    <div>
                        <b>${money(u.total_wager)}</b>
                        <span>Total Wager</span>
                    </div>

                    <div>
                        <b>${money(u.balance)}</b>
                        <span>Balance</span>
                    </div>

                    <div>
                        <b>${u.wins}</b>
                        <span>Wins</span>
                    </div>

                    <div>
                        <b>${u.losses}</b>
                        <span>Losses</span>
                    </div>

                </div>

                <div class="profile-history-title">
                    BETTING HISTORY
                </div>

                <div class="profile-history">

                    ${
                        history.length
                        ? history.map(h => `
                            <div class="history-card">

                                <div class="history-card-user">

                                    <img
                                        src="${escapeHtml(
                                            h.opponent?.avatar ||
                                            "https://cdn.discordapp.com/embed/avatars/0.png"
                                        )}"
                                    >

                                    <div>
                                        <b>
                                            ${escapeHtml(
                                                h.opponent?.display_name ||
                                                "Unknown"
                                            )}
                                        </b>

                                        <small>
                                            ${formatChatTime(h.finished_at)}
                                        </small>
                                    </div>

                                </div>

                                <div>
                                    <b>${money(h.bet)}</b>
                                    <small
                                        class="${
                                            h.result === "WIN"
                                            ? "history-win"
                                            : "history-loss"
                                        }"
                                    >
                                        ${h.result}
                                    </small>
                                </div>

                            </div>
                        `).join("")
                        : `
                            <div class="empty">
                                No completed bets yet.
                            </div>
                        `
                    }

                </div>

            </div>
            `
        );

    } catch (error) {

        console.error(
            "[PROFILE ERROR]",
            error
        );

        toast("Could not load profile.");
    }
}


async function sendTipTo(userId, displayName) {
    const raw = prompt(`Tip ${displayName} how many Gems?`);
    if (raw === null) return;
    const amount = Number(raw);
    if (!Number.isFinite(amount) || amount <= 0) { toast("Enter a valid tip amount."); return; }
    try {
        const response = await fetch("/api/tip", {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"}, body:JSON.stringify({user_id:userId, amount})});
        const data = await response.json();
        if (!response.ok || !data.ok) { toast(data.error || "Tip failed."); return; }
        const bal = document.getElementById("topBalance");
        if (bal) bal.textContent = Number(data.balance).toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
        toast("Tip sent!");
        loadChat();
    } catch(e) { console.error(e); toast("Tip failed."); }
}


async function openGiveItem(userId, displayName){
    const inv=await fetch("/api/inventory",{credentials:"same-origin"}).then(r=>r.json()).catch(()=>({}));
    if(!inv.ok){toast(inv.error||"Could not load inventory.");return;}
    window.__giveTargetId=Number(userId); window.__giveTargetName=displayName; window.__giveItems=inv.items||[]; window.__giveSelected=[];
    showGeneral(`Give Item · ${escapeHtml(displayName)}`,`
      <input id="giveSearch" class="inventory-search" style="width:100%;box-sizing:border-box;margin-bottom:10px" placeholder="Search for an item..." oninput="renderGiveItems()">
      <div id="giveItemGrid" class="inventory-grid item-give-grid"></div>
      <div class="give-code-box"><input id="givePermissionCode" type="password" placeholder="Enter permission code"><button class="primary" onclick="submitGiveItem()">Give</button></div>`);
    renderGiveItems();
}
function renderGiveItems(){
    const grid=document.getElementById("giveItemGrid");if(!grid)return;const search=(document.getElementById("giveSearch")?.value||"").toLowerCase();const list=window.__giveItems.filter(x=>!search||String(x.name).toLowerCase().includes(search));
    grid.innerHTML=list.length?list.map(x=>`<button class="inventory-item ${window.__giveSelected.includes(Number(x.id))?"selected":""}" onclick="toggleGiveItem(${Number(x.id)})"><span class="inventory-item-count">x${Number(x.count||1)}</span><img src="${escapeHtml(x.icon||"")}"><div class="inventory-item-name">${escapeHtml(x.name)}</div><div class="inventory-item-value">💎 ${escapeHtml(formatCompact(x.value))}</div></button>`).join(""):"<div class=inventory-empty>No items found.</div>";
}
function toggleGiveItem(id){const i=window.__giveSelected.indexOf(Number(id));if(i>=0)window.__giveSelected.splice(i,1);else window.__giveSelected.push(Number(id));renderGiveItems();}
async function submitGiveItem(){
    if(!window.__giveSelected?.length){toast("Select at least one item.");return;}const code=document.getElementById("givePermissionCode")?.value||"";if(!code){toast("Enter the permission code.");return;}
    const response=await fetch("/api/give-item",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({user_id:window.__giveTargetId,item_ids:window.__giveSelected,code})});const data=await response.json();
    if(!response.ok||!data.ok){toast(data.error||"Give Item failed.");return;}closeModal("generalOverlay");toast(data.message||"Items given!");await loadChat();await refreshMe();
}

async function sendChat() {

    const input =
        document.getElementById(
            "chatInput"
        );

    if(input.disabled) {

        toast(
            "Login with Discord first."
        );

        return;
    }

    const message =
        input.value.trim();

    if(!message)
        return;

    const response =
        await fetch(
            "/api/chat",
            {
                method: "POST",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body:
                    JSON.stringify({
                        message
                    })
            }
        );

    const data =
        await response.json();

    if(
        !response.ok
        || !data.ok
    ) {

        toast(
            data.error ||
            "Could not send."
        );

        return;
    }

    input.value = "";

    loadChat();
}


async function loadWalletItems(){
    try{
        const response=await fetch("/api/inventory",{credentials:"same-origin",cache:"no-store"}); const data=await response.json();
        const grid=document.getElementById("walletItems"); if(!grid)return data;
        if(!response.ok||!data.ok){grid.innerHTML=`<div class="inventory-empty">${escapeHtml(data.error||"Could not load inventory.")}</div>`;return data;}
        const search=(document.getElementById("walletSearch")?.value||"").toLowerCase(); const items=(data.items||[]).filter(x=>!search||String(x.name).toLowerCase().includes(search));
        grid.innerHTML=items.length?items.map(x=>`<div class="wallet-item-card"><img src="${escapeHtml(x.icon||"")}"><b>${escapeHtml(x.name)}</b><span>💎 ${escapeHtml(formatCompact(x.value))} · x${Number(x.count||1)}</span></div>`).join(""):"<div class=inventory-empty>No items found.</div>";
        const w=document.getElementById("walletTotalValue"),c=document.getElementById("walletItemCount"); if(w)w.textContent=formatCompact(data.total_value||0); if(c)c.textContent=Number(data.item_count||0);
        return data;
    }catch(e){console.error(e);return null;}
}

async function showWallet(){
    const me=await fetch("/api/me",{credentials:"same-origin"}).then(r=>r.json()).catch(()=>({}));
    if(!me.logged_in){toast("Login with Discord first.");return;}
    showGeneral("Wallet",`
      <div class="wallet-panel">
        <div class="wallet-panel-top"><div><div class="wallet-sub">GEMS AVAILABLE</div><div class="wallet-value">💎 <span id="walletGemBalance">${escapeHtml(formatCompact(me.balance||0))}</span></div><div class="wallet-sub">Virtual Pet Simulator 99 inventory</div></div><button class="primary" onclick="syncInventory()">＋ Deposit Items</button></div>
        <div class="wallet-toolbar"><input id="walletSearch" placeholder="Search for an item..." oninput="loadWalletItems()"><select class="inventory-sort"><option>Highest to Lowest</option></select></div>
        <div class="wallet-items" id="walletItems"><div class="inventory-empty">Loading inventory...</div></div>
      </div>`);
    await loadWalletItems();
}


async function showLeaderboard() {

    const response =
        await fetch(
            "/api/leaderboard"
        );

    const users =
        await response.json();

    showGeneral(
        "Leaderboard",
        `
        <div
            style="
                display:flex;
                flex-direction:column;
                gap:8px;
            "
        >

        ${
            users.map(
                (u,i) =>
                    `
                    <div
                        style="
                            display:grid;
                            grid-template-columns:
                                35px 1fr auto;
                            gap:10px;
                            align-items:center;
                            padding:12px;
                            background:#101827;
                            border:
                                1px solid #243149;
                            border-radius:10px;
                        "
                    >

                        <b>
                            #${i+1}
                        </b>

                        <div
                            style="
                                display:flex;
                                align-items:center;
                                gap:9px;
                            "
                        >

                            <img
                                src="${escapeHtml(
                                    u.avatar
                                )}"
                                style="
                                    width:34px;
                                    height:34px;
                                    border-radius:9px;
                                "
                            >

                            <div>

                                <b>
                                    ${escapeHtml(
                                        u.display_name
                                    )}
                                </b>

                                <div
                                    style="
                                        font-size:10px;
                                        color:#7d8da6;
                                    "
                                >
                                    ${u.wins}W /
                                    ${u.losses}L
                                </div>

                            </div>

                        </div>

                        <b
                            style="
                                color:#69d7ff;
                            "
                        >
                            ${money(u.balance)}
                        </b>

                    </div>
                    `
            ).join("")
        }

        </div>
        `
    );
}


function showHistory() {

    {% if user %}

    showGeneral(
        "Your History",
        `
        <div
            style="
                text-align:center;
                color:#8795aa;
                padding:30px;
            "
        >
            Your completed Color Dice
            matches can be opened through
            the View button.
        </div>
        `
    );

    {% else %}

    toast(
        "Login with Discord first."
    );

    {% endif %}
}


function showProfile() {

    {% if user %}

    showGeneral(
        "Profile",
        `
        <div
            style="text-align:center;"
        >

            <img
                src="{{ user['avatar'] }}"
                style="
                    width:100px;
                    height:100px;
                    border-radius:50%;
                    border:
                        2px solid #34445d;
                "
            >

            <h2>
                {{ user["display_name"]|e }}
            </h2>

            <p
                style="
                    color:#8492aa;
                "
            >
                @{{ user["username"]|e }}
            </p>

            <div
                style="
                    display:grid;
                    grid-template-columns:
                        repeat(3,1fr);
                    gap:8px;
                    margin-top:22px;
                "
            >

                <div
                    style="
                        padding:15px;
                        background:#101827;
                        border-radius:10px;
                    "
                >
                    <b>
                        {{ user["games"] }}
                    </b>
                    <br>
                    <small
                        style="
                            color:#8290a7;
                        "
                    >
                        Games
                    </small>
                </div>

                <div
                    style="
                        padding:15px;
                        background:#101827;
                        border-radius:10px;
                    "
                >
                    <b>
                        {{ user["wins"] }}
                    </b>
                    <br>
                    <small
                        style="
                            color:#8290a7;
                        "
                    >
                        Wins
                    </small>
                </div>

                <div
                    style="
                        padding:15px;
                        background:#101827;
                        border-radius:10px;
                    "
                >
                    <b>
                        {{ user["losses"] }}
                    </b>
                    <br>
                    <small
                        style="
                            color:#8290a7;
                        "
                    >
                        Losses
                    </small>
                </div>

            </div>

        </div>
        `
    );

    {% else %}

    toast(
        "Login with Discord first."
    );

    {% endif %}
}


function showGeneral(
    title,
    content
) {

    document.getElementById(
        "generalTitle"
    ).textContent = title;

    document.getElementById(
        "generalContent"
    ).innerHTML = content;

    document
        .getElementById(
            "generalOverlay"
        )
        .classList.add("open");
}


setInterval(
    loadMatches,
    250
);

setInterval(
    loadChat,
    2500
);

setInterval(
    refreshMe,
    4000
);

async function loadLobbyLiveWins(){
    const container = document.getElementById('lobbyLiveWins');
    if (!container) return;
    try {
        const response = await fetch('/api/live-wins', { credentials: 'same-origin', cache: 'no-store' });
        if (!response.ok) throw new Error('Could not load recent wins');
        const wins = await response.json();
        if (!Array.isArray(wins) || wins.length === 0) {
            container.innerHTML = '<div class="lobby-win-empty">🎲 아직 표시할 최근 승리가 없습니다. 플레이어가 경기를 이기면 여기에 표시됩니다.</div>';
            return;
        }
        container.innerHTML = wins.map(win => {
            const name = escapeHtml(win.display_name || win.username || 'Player');
            const avatar = win.avatar
                ? `<img class="lobby-win-avatar" src="${escapeHtml(win.avatar)}" alt="" loading="lazy" onerror="this.outerHTML='&lt;div class=&quot;lobby-win-avatar&quot;&gt;🎲&lt;/div&gt;'">`
                : '<div class="lobby-win-avatar">🎲</div>';
            const payout = Number(win.payout || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
            const mode = win.match_mode === '1v1v1' ? '3-player match' : (Number(win.best_of || 1) > 1 ? `First to ${Number(win.best_of)}` : 'ColorDice');
            return `<article class="lobby-win-card">${avatar}<div class="lobby-win-info"><div class="lobby-win-name">${name}</div><div class="lobby-win-detail">🏆 ${mode} · Bet $${Number(win.bet || 0).toLocaleString()}</div></div><div class="lobby-win-payout">+$${payout}</div></article>`;
        }).join('');
    } catch (error) {
        container.innerHTML = '<div class="lobby-win-empty">최근 승리를 불러오지 못했습니다. 잠시 후 다시 시도합니다.</div>';
    }
}

function showLobby(){
    const lobby=document.getElementById('lobbyHome');
    const game=document.getElementById('gameApp');
    if(lobby) lobby.style.display='block';
    if(game) game.style.display='none';
    window.scrollTo({top:0,behavior:'smooth'});
}
function enterGame(){
    const lobby=document.getElementById('lobbyHome');
    const game=document.getElementById('gameApp');
    if(lobby) lobby.style.display='none';
    if(game) game.style.display='block';
    window.scrollTo({top:0,behavior:'smooth'});
    loadMatches();
}

loadMatches();
loadChat();
refreshMe();
loadLobbyLiveWins();
setInterval(loadLobbyLiveWins, 5000);

</script>

</body>

</html>
"""


# ============================================================
# HOME
# ============================================================

@app.route("/")
def index():

    return render_template_string(
        PAGE,
        user=current_user(),
        colors=COLORS,
        gem_value=gem_value
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print("=" * 60)

    print(
        " ColorDice started"
    )

    print(
        " Discord OAuth + 1v1 Color Dice"
    )

    print(
        " Virtual Gems + PS99 item system"
    )

    print(
        " http://127.0.0.1:5000"
    )

    print("=" * 60)

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "5000"
            )
        ),
        debug=True
    )
