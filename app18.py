import os
import secrets
import sqlite3
import random
import time
import json
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import (
    Flask, request, redirect, session, jsonify,
    render_template_string, url_for
)

# ============================================================
# ColorDice - Single File Virtual 1v1 Color Dice Demo
# ============================================================
# This version uses VIRTUAL Gems only.
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
        created_at INTEGER NOT NULL
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

    CREATE TABLE IF NOT EXISTS inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        item_id TEXT NOT NULL,
        item_name TEXT NOT NULL,
        image_url TEXT,
        value REAL NOT NULL DEFAULT 0,
        quantity INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        UNIQUE(user_id, item_id)
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

    match_columns = [
        row["name"]
        for row in conn.execute("PRAGMA table_info(matches)").fetchall()
    ]

    for column, definition in [
        ("creator_items", "TEXT NOT NULL DEFAULT '[]'"),
        ("opponent_items", "TEXT NOT NULL DEFAULT '[]'"),
        ("third_items", "TEXT NOT NULL DEFAULT '[]'"),
    ]:
        if column not in match_columns:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {column} {definition}")

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
    return "💎 " + format_gems(value)


def format_gems(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    if abs(value) >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f}".rstrip("0").rstrip(".") + "T"
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}".rstrip("0").rstrip(".") + "B"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.2f}".rstrip("0").rstrip(".") + "K"
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


app.jinja_env.globals["format_gems"] = format_gems


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


def normalize_api_image(value):
    if not value:
        return ""
    value = str(value)
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("rbxassetid://"):
        asset_id = value.split("//", 1)[1].strip()
        if asset_id.isdigit():
            return f"https://ps99.biggamesapi.io/image/{asset_id}"
    if value.isdigit():
        return f"https://ps99.biggamesapi.io/image/{value}"
    return value


def parse_item_list(value):
    try:
        data = json.loads(value or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def normalize_wager_items(items):
    normalized = []
    for item in items or []:
        x = dict(item)
        x["image"] = normalize_api_image(x.get("image") or x.get("image_url"))
        normalized.append(x)
    return normalized


def item_total_value(items):
    total = 0.0
    for item in items or []:
        try:
            total += float(item.get("value", 0)) * int(item.get("quantity", 1))
        except Exception:
            pass
    return total


def inventory_rows(user_id):
    conn = db()
    rows = conn.execute("SELECT * FROM inventory_items WHERE user_id=? AND quantity>0 ORDER BY value DESC, item_name ASC", (user_id,)).fetchall()
    conn.close()
    items = []
    for r in rows:
        item = dict(r)
        item["image_url"] = normalize_api_image(item.get("image_url"))
        items.append(item)
    return items


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

        "creator_items": normalize_wager_items(parse_item_list(row["creator_items"])) if "creator_items" in row.keys() else [],
        "opponent_items": normalize_wager_items(parse_item_list(row["opponent_items"])) if "opponent_items" in row.keys() else [],
        "third_items": normalize_wager_items(parse_item_list(row["third_items"])) if "third_items" in row.keys() else [],
        "creator_item_value": item_total_value(parse_item_list(row["creator_items"])) if "creator_items" in row.keys() else 0,
        "opponent_item_value": item_total_value(parse_item_list(row["opponent_items"])) if "opponent_items" in row.keys() else 0,
        "third_item_value": item_total_value(parse_item_list(row["third_items"])) if "third_items" in row.keys() else 0,

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
# PS99 PUBLIC CATALOG + VIRTUAL INVENTORY
# ============================================================

BIG_GAMES_API = "https://ps99.biggamesapi.io"

# Virtual-only Give Item permission. Set GIVE_ITEM_CODE in the hosting
# environment for a permanent code. If it is omitted, a one-session code is
# generated and printed once to the server console.
GIVE_ITEM_CODE = os.getenv("GIVE_ITEM_CODE", "6974").strip()
if not GIVE_ITEM_CODE:
    GIVE_ITEM_CODE = secrets.token_urlsafe(10)
    print(f"[GIVE ITEM] Permission code: {GIVE_ITEM_CODE}")


def fetch_big_games_json(path):
    response = requests.get(BIG_GAMES_API + path, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise RuntimeError(payload.get("error", {}).get("message", "BIG Games API error"))
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def catalog_item_from_api(raw, rap_map):
    if not isinstance(raw, dict):
        return None
    cfg = raw.get("configData") if isinstance(raw.get("configData"), dict) else {}
    item_id = str(raw.get("configName") or raw.get("id") or cfg.get("id") or "").strip()
    if not item_id:
        return None
    name = str(raw.get("displayName") or cfg.get("displayName") or raw.get("configName") or item_id)
    thumb = normalize_api_image(raw.get("thumbnail") or cfg.get("thumbnail") or cfg.get("icon") or raw.get("icon"))
    value = raw.get("value") or raw.get("rap") or cfg.get("value") or cfg.get("rap") or rap_map.get(item_id, 0)
    if not value:
        value = rap_map.get(name, 0)
    try:
        value = float(value or 0)
    except Exception:
        value = 0.0
    return {"item_id": item_id, "name": name, "image": thumb, "value": value, "category": str(raw.get("category") or cfg.get("category") or "Pets")}


_PS99_CATALOG_CACHE = {"items": [], "expires": 0}


def load_ps99_catalog(force=False):
    now = time.time()
    if not force and _PS99_CATALOG_CACHE["items"] and now < _PS99_CATALOG_CACHE["expires"]:
        return _PS99_CATALOG_CACHE["items"]

    # BIG Games' public legacy API is the authoritative source for PS99
    # configuration, RAP and exists data.  We only copy catalog metadata into
    # the site's virtual inventory; no Roblox inventory is accessed.
    pets = fetch_big_games_json("/api/collection/Pets")
    rap = fetch_big_games_json("/api/rap")
    try:
        exists = fetch_big_games_json("/api/exists")
    except Exception:
        exists = []

    def rows_from(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return []

    rap_map = {}
    for row in rows_from(rap):
        if not isinstance(row, dict):
            continue
        cfg = row.get("configData") if isinstance(row.get("configData"), dict) else {}
        value = row.get("value")
        try:
            value = float(value or 0)
        except Exception:
            continue
        for key in (row.get("configName"), row.get("id"), cfg.get("id"), cfg.get("name"), row.get("displayName")):
            if key:
                rap_map[str(key).strip().lower()] = value

    exists_map = {}
    for row in rows_from(exists):
        if not isinstance(row, dict):
            continue
        cfg = row.get("configData") if isinstance(row.get("configData"), dict) else {}
        value = row.get("value")
        try:
            value = float(value or 0)
        except Exception:
            continue
        for key in (row.get("configName"), row.get("id"), cfg.get("id"), cfg.get("name")):
            if key:
                exists_map[str(key).strip().lower()] = value

    result = []
    for raw in rows_from(pets):
        if not isinstance(raw, dict):
            continue
        cfg = raw.get("configData") if isinstance(raw.get("configData"), dict) else {}
        item_id = str(raw.get("configName") or cfg.get("name") or raw.get("id") or cfg.get("id") or "").strip()
        if not item_id:
            continue
        name = str(cfg.get("name") or raw.get("displayName") or raw.get("configName") or item_id)
        thumb = normalize_api_image(cfg.get("thumbnail") or raw.get("thumbnail") or cfg.get("icon") or raw.get("icon"))
        key_candidates = [item_id.lower(), name.lower()]
        value = 0.0
        for key in key_candidates:
            if key in rap_map:
                value = rap_map[key]
                break
        if not value:
            for key in key_candidates:
                if key in exists_map:
                    value = exists_map[key]
                    break
        result.append({
            "item_id": item_id,
            "name": name,
            "image": thumb,
            "value": float(value or 0),
            "category": str(raw.get("category") or cfg.get("category") or "Pets"),
        })

    result.sort(key=lambda x: (-float(x["value"]), x["name"].lower()))
    _PS99_CATALOG_CACHE["items"] = result
    _PS99_CATALOG_CACHE["expires"] = time.time() + 60
    return result


@app.route("/api/ps99/catalog")
def api_ps99_catalog():
    try:
        force = request.args.get("refresh") == "1"
        return jsonify({"ok": True, "items": load_ps99_catalog(force=force)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not load BIG Games catalog: {exc}"}), 502


@app.route("/api/inventory")
@login_required
def api_inventory():
    return jsonify({"ok": True, "items": inventory_rows(current_user()["id"])})


@app.route("/api/inventory/deposit", methods=["POST"])
@login_required
def api_inventory_deposit():
    user = current_user()
    data = request.get_json(silent=True) or {}
    item_id = str(data.get("item_id", "")).strip()
    quantity = max(1, min(int(data.get("quantity", 1) or 1), 1000))
    if not item_id:
        return jsonify({"ok": False, "error": "Choose an item."}), 400
    try:
        catalog = load_ps99_catalog()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"BIG Games API unavailable: {exc}"}), 502
    item = next((x for x in catalog if x["item_id"] == item_id), None)
    if not item:
        return jsonify({"ok": False, "error": "Item not found in the BIG Games catalog."}), 404
    conn = db()
    conn.execute("""INSERT INTO inventory_items(user_id,item_id,item_name,image_url,value,quantity,created_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity, item_name=excluded.item_name, image_url=excluded.image_url, value=excluded.value""",
                 (user["id"], item["item_id"], item["name"], item["image"], item["value"], quantity, int(time.time())))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "item": item, "quantity": quantity})


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

    data = (
        request.get_json(
            silent=True
        ) or {}
    )

    try:
        bet = float(data.get("bet", 0) or 0)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid Gems amount."}), 400

    wager_items = data.get("items", [])
    if not isinstance(wager_items, list):
        wager_items = []
    clean_item_ids = [str(x) for x in wager_items if str(x).strip()]

    raw_mode = str(data.get("mode", "1v1"))
    match_mode = "1v1v1" if raw_mode == "1v1v1" else "1v1"
    try:
        best_of = int(data.get("best_of", 1))
    except (TypeError, ValueError):
        best_of = 1
    if best_of not in (1, 2, 3, 5):
        best_of = 1

    colors = data.get(
        "colors",
        []
    )

    # Backwards compatibility
    if not colors:

        old_color = data.get(
            "color"
        )

        if old_color:
            colors = [old_color]

    if not isinstance(colors, list):

        colors = [colors]

    clean_colors = []

    for color in colors:

        color = str(
            color
        ).lower()

        if color in COLORS:
            if color not in clean_colors:
                clean_colors.append(color)

    if len(clean_colors) < 1:

        return jsonify({
            "ok": False,
            "error": "Choose at least one color."
        }), 400

    if len(clean_colors) > 2:

        return jsonify({
            "ok": False,
            "error": "You can choose up to 2 colors."
        }), 400

    if bet < 0:
        return jsonify({"ok": False, "error": "Invalid Gems wager."}), 400

    if bet > MAX_BET:
        return jsonify({"ok": False, "error": "Maximum Gems wager is 500M."}), 400

    conn = db()

    selected_items = []
    if clean_item_ids:
        placeholders = ",".join("?" for _ in clean_item_ids)
        item_rows = conn.execute(f"SELECT * FROM inventory_items WHERE user_id=? AND item_id IN ({placeholders}) AND quantity>0", [user["id"], *clean_item_ids]).fetchall()
        by_id = {r["item_id"]: r for r in item_rows}
        for iid in clean_item_ids:
            r = by_id.get(iid)
            if not r:
                conn.close(); return jsonify({"ok": False, "error": "One or more wager items are not in your virtual wallet."}), 400
            selected_items.append({"item_id": r["item_id"], "name": r["item_name"], "image": r["image_url"], "value": float(r["value"]), "quantity": 1})
    if bet <= 0 and not selected_items:
        conn.close(); return jsonify({"ok": False, "error": "Add Gems or at least one wager item."}), 400

    active = conn.execute(
        """
        SELECT id
        FROM matches
        WHERE creator_id = ?
        AND status IN
            ('waiting', 'ready')
        """,
        (user["id"],)
    ).fetchone()

    if active:

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "You already have an active match."
        }), 400

    fresh = conn.execute(
        """
        SELECT balance
        FROM users
        WHERE id = ?
        """,
        (user["id"],)
    ).fetchone()

    if (
        not fresh
        or fresh["balance"] < bet
    ):

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "Insufficient virtual balance."
        }), 400

    conn.execute(
        """
        UPDATE users
        SET
            balance = balance - ?,
            total_wager = total_wager + ?
        WHERE id = ?
        """,
        (
            bet,
            bet,
            user["id"]
        )
    )

    cursor = conn.execute(
        """
        INSERT INTO matches
        (
            creator_id,
            bet,
            creator_color,
            status,
            created_at,
            opponent_is_bot,
            match_mode,
            best_of,
            creator_items
        )
        VALUES (?, ?, ?, 'waiting', ?, 0, ?, ?, ?)
        """,
        (
            user["id"],
            bet,
            save_colors(clean_colors),
            int(time.time()),
            match_mode,
            best_of,
            json.dumps(selected_items)
        )
    )

    match_id = cursor.lastrowid

    for item in selected_items:
        conn.execute("UPDATE inventory_items SET quantity=quantity-1 WHERE user_id=? AND item_id=? AND quantity>0", (user["id"], item["item_id"]))

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "match_id": match_id
    })


# ============================================================
# CANCEL MATCH
# ============================================================

@app.route(
    "/api/matches/<int:match_id>/cancel",
    methods=["POST"]
)
@login_required
def cancel_match(match_id):

    user = current_user()

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

        return jsonify({
            "ok": False,
            "error": "Match not found."
        }), 404

    if row["creator_id"] != user["id"]:

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "Only the creator can cancel this match."
        }), 403

    if row["status"] != "waiting":

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "This match can no longer be cancelled."
        }), 400

    # Refund creator Gems and virtual wager items.
    creator_items = parse_item_list(row["creator_items"]) if "creator_items" in row.keys() else []
    for item in creator_items:
        conn.execute("""INSERT INTO inventory_items(user_id,item_id,item_name,image_url,value,quantity,created_at)
                        VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity, item_name=excluded.item_name, image_url=excluded.image_url, value=excluded.value""",
                     (user["id"], item.get("item_id", ""), item.get("name", item.get("item_id", "Item")), item.get("image", ""), float(item.get("value", 0) or 0), int(item.get("quantity", 1) or 1), int(time.time())))

    conn.execute(
        """
        UPDATE users
        SET balance = balance + ?
        WHERE id = ?
        """,
        (
            row["bet"],
            user["id"]
        )
    )

    conn.execute(
        """
        UPDATE matches
        SET status = 'cancelled',
            finished_at = ?
        WHERE id = ?
        """,
        (
            int(time.time()),
            match_id
        )
    )

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "message":
            "Match cancelled and wager refunded."
    })


# ============================================================
# JOIN PLAYER
# ============================================================

@app.route(
    "/api/matches/<int:match_id>/join",
    methods=["POST"]
)
@login_required
def join_match(match_id):

    user = current_user()

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

        return jsonify({
            "ok": False,
            "error": "Match not found."
        }), 404

    is_three = (row["match_mode"] == "1v1v1")
    waiting_for_third = is_three and row["opponent_id"] and not row["third_player_id"]

    if row["status"] != "waiting" or (is_three and row["opponent_id"] and row["third_player_id"]):
        conn.close()
        return jsonify({"ok": False, "error": "This match is no longer waiting."}), 400

    if row["creator_id"] == user["id"] or row["opponent_id"] == user["id"]:


        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "You cannot join your own match."
        }), 400

    active = conn.execute(
        """
        SELECT id
        FROM matches
        WHERE opponent_id = ?
        AND status IN ('waiting', 'ready')
        """,
        (user["id"],)
    ).fetchone()

    if active:

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "You already have an active match."
        }), 400

    fresh = conn.execute(
        """
        SELECT balance
        FROM users
        WHERE id = ?
        """,
        (user["id"],)
    ).fetchone()

    if (
        not fresh
        or fresh["balance"] < row["bet"]
    ):

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "Insufficient virtual balance."
        }), 400

    creator_colors = parse_colors(
        row["creator_color"]
    )

    available = [
        color
        for color in COLOR_NAMES
        if color not in creator_colors
    ]

    data = request.get_json(silent=True) or {}
    wager_items = data.get("items", [])
    if not isinstance(wager_items, list):
        wager_items = []
    wager_item_ids = [str(x) for x in wager_items if str(x).strip()]
    selected_items = []
    if wager_item_ids:
        placeholders = ",".join("?" for _ in wager_item_ids)
        item_rows = conn.execute(f"SELECT * FROM inventory_items WHERE user_id=? AND item_id IN ({placeholders}) AND quantity>0", [user["id"], *wager_item_ids]).fetchall()
        by_id = {r["item_id"]: r for r in item_rows}
        for iid in wager_item_ids:
            r = by_id.get(iid)
            if not r:
                conn.close(); return jsonify({"ok": False, "error": "One or more wager items are not in your virtual wallet."}), 400
            selected_items.append({"item_id": r["item_id"], "name": r["item_name"], "image": r["image_url"], "value": float(r["value"]), "quantity": 1})

    chosen = data.get("colors", [])
    if not isinstance(chosen, list):
        chosen = [chosen]
    chosen = list(dict.fromkeys(str(c).lower() for c in chosen if str(c).lower() in COLORS))
    if len(chosen) != len(creator_colors):
        conn.close()
        return jsonify({"ok": False, "error": f"Choose exactly {len(creator_colors)} color(s)."}), 400
    if set(chosen) & set(creator_colors):
        conn.close()
        return jsonify({"ok": False, "error": "Choose colors not already taken in this match."}), 400
    if waiting_for_third and set(chosen) & set(parse_colors(row["opponent_color"])):
        conn.close()
        return jsonify({"ok": False, "error": "Those colors are already taken."}), 400

    conn.execute(
        "UPDATE users SET balance = balance - ?, total_wager = total_wager + ? WHERE id = ?",
        (row["bet"], row["bet"], user["id"])
    )
    for item in selected_items:
        conn.execute("UPDATE inventory_items SET quantity=quantity-1 WHERE user_id=? AND item_id=? AND quantity>0", (user["id"], item["item_id"]))
    if waiting_for_third:
        conn.execute(
            "UPDATE matches SET third_player_id = ?, third_color = ?, third_items = ?, status = 'ready' WHERE id = ?",
            (user["id"], save_colors(chosen), json.dumps(selected_items), match_id)
        )
        new_status = "ready"
    else:
        conn.execute(
            "UPDATE matches SET opponent_id = ?, opponent_color = ?, opponent_items = ?, opponent_is_bot = 0, status = ? WHERE id = ?",
            (user["id"], save_colors(chosen), json.dumps(selected_items), "ready" if not is_three else "waiting", match_id)
        )
        new_status = "ready" if not is_three else "waiting"
    conn.commit()
    conn.close()

    # The original ColorDice flow starts automatically as soon as the required
    # players are present. The browser only opens/replays the animation; there
    # is no manual START GAME step.
    final_match = settle_match(match_id) if new_status == "ready" else None
    conn2 = db()
    final = conn2.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    conn2.close()
    return jsonify({
        "ok": True,
        "match_id": match_id,
        "status": final["status"] if final else new_status,
        "match": match_json(final),
        "started": bool(final_match)
    })


@app.route(
    "/api/matches/<int:match_id>/call-bot",
    methods=["POST"]
)
@login_required
def call_bot(match_id):

    user = current_user()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Login with Discord first."
        }), 401

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

        return jsonify({
            "ok": False,
            "error": "Match not found."
        }), 404

    print(
        f"[CALL BOT] match={match_id} "
        f"creator={row['creator_id']} "
        f"current_user={user['id']}"
    )

    # 반드시 생성자 본인만 Call Bot 가능
    if int(row["creator_id"]) != int(user["id"]):

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "Only the creator can call the bot."
        }), 403

    if row["status"] != "waiting":

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "This match is no longer waiting."
        }), 400

    creator_colors = parse_colors(
        row["creator_color"]
    )

    if not creator_colors:

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "Creator colors are missing."
        }), 400

    available_colors = [
        color
        for color in COLOR_NAMES
        if color not in creator_colors
    ]

    if not available_colors:

        conn.close()

        return jsonify({
            "ok": False,
            "error":
                "There are no colors available for the bot."
        }), 400

    # --------------------------------------------------------
    # 1 color 선택
    # → 남은 5색 중 1색
    #
    # 2 color 선택
    # → 남은 4색 중 2색
    # --------------------------------------------------------

    bot_color_count = min(
        len(creator_colors),
        len(available_colors)
    )

    bot_colors = random.sample(
        available_colors,
        bot_color_count
    )

    print(
        f"[CALL BOT] "
        f"player={creator_colors} "
        f"bot={bot_colors}"
    )

    if row["match_mode"] == "1v1v1":
        remaining = [c for c in COLOR_NAMES if c not in creator_colors and c not in bot_colors]
        if not remaining:
            conn.close()
            return jsonify({"ok": False, "error": "Not enough colors for two bots."}), 400
        bot2_colors = random.sample(remaining, min(len(creator_colors), len(remaining)))
        conn.execute(
            """UPDATE matches SET opponent_id=NULL, opponent_color=?, opponent_is_bot=1,
               third_player_id=NULL, third_color=?, third_is_bot=1, status='ready' WHERE id=?""",
            (save_colors(bot_colors), save_colors(bot2_colors), match_id)
        )
    else:
        conn.execute(
            """UPDATE matches SET opponent_id=NULL, opponent_color=?, opponent_is_bot=1, status='ready' WHERE id=?""",
            (save_colors(bot_colors), match_id)
        )
    conn.commit()
    conn.close()

    # Call Bot follows the original automatic-start behavior. The match is
    # settled server-side immediately, while the frontend replays the stored
    # roll history with the normal countdown/reveal animation.
    final_match = settle_match(match_id)
    if not final_match:
        return jsonify({"ok": False, "error": "Could not start the bot match."}), 500

    return jsonify({
        "ok": True,
        "match_id": match_id,
        "bot_colors": bot_colors,
        "status": "finished",
        "started": True
    })


# ============================================================
# START MATCH
# ============================================================

@app.route("/api/matches/<int:match_id>/start", methods=["POST"])
@login_required
def start_match(match_id):
    user = current_user()
    conn = db()
    row = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"ok": False, "error": "Match not found."}), 404
    if row["creator_id"] != user["id"]:
        conn.close(); return jsonify({"ok": False, "error": "Only the creator can start the match."}), 403
    if row["status"] != "ready":
        conn.close(); return jsonify({"ok": False, "error": "Match is not ready yet."}), 400
    conn.close()
    final = settle_match(match_id)
    if not final:
        return jsonify({"ok": False, "error": "Could not start match."}), 500
    return jsonify({"ok": True, "status": "finished", "match_id": match_id})


# ============================================================
# SETTLE MATCH
# ============================================================

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

    # Transfer virtual wager items to the winner. Bot items are not materialized.
    if winner > 0:
        player_ids = [creator]
        if opponent and opponent > 0:
            player_ids.append(opponent)
        if row["third_player_id"] and row["third_player_id"] > 0:
            player_ids.append(row["third_player_id"])
        all_wager_items = []
        for col in ("creator_items", "opponent_items", "third_items"):
            if col in row.keys():
                all_wager_items.extend(parse_item_list(row[col]))
        for item in all_wager_items:
            iid = str(item.get("item_id", ""))
            qty = int(item.get("quantity", 1) or 1)
            if not iid or qty < 1:
                continue
            conn.execute("""INSERT INTO inventory_items(user_id,item_id,item_name,image_url,value,quantity,created_at)
                            VALUES(?,?,?,?,?,?,?)
                            ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity, item_name=excluded.item_name, image_url=excluded.image_url, value=excluded.value""",
                         (winner, iid, item.get("name", iid), item.get("image", ""), float(item.get("value", 0) or 0), qty, int(time.time())))

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
        return jsonify({"ok": False, "error": "Tip must be between 1 and 1M Gems."}), 400
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
    public_message = f"💎 {sender_name} sent {recipient['display_name']} {format_gems(amount)} Gems!"
    conn.execute("INSERT INTO chat(user_id,message,created_at) VALUES(?,?,?)", (sender["id"], public_message, int(time.time())))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": public_message, "balance": float(balance["balance"])-amount})


# ============================================================
# VIRTUAL GIVE ITEM
# ============================================================

@app.route("/api/give-items", methods=["POST"])
@login_required
def api_give_items():
    sender = current_user()
    data = request.get_json(silent=True) or {}

    try:
        recipient_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid recipient."}), 400

    code = str(data.get("code", "")).strip()
    if not secrets.compare_digest(code, GIVE_ITEM_CODE):
        return jsonify({"ok": False, "error": "Invalid permission code."}), 403

    if recipient_id == int(sender["id"]):
        return jsonify({"ok": False, "error": "You cannot give items to yourself."}), 400

    item_ids = data.get("items", [])
    if not isinstance(item_ids, list):
        item_ids = [item_ids]
    item_ids = list(dict.fromkeys(str(x).strip() for x in item_ids if str(x).strip()))
    if not item_ids:
        return jsonify({"ok": False, "error": "Select at least one item."}), 400
    if len(item_ids) > 100:
        return jsonify({"ok": False, "error": "You can give up to 100 items at once."}), 400

    conn = db()
    recipient = conn.execute("SELECT id, display_name FROM users WHERE id=?", (recipient_id,)).fetchone()
    if not recipient:
        conn.close()
        return jsonify({"ok": False, "error": "User not found."}), 404

    # Give Item is a permission-protected virtual grant. The authorized user
    # can choose any item from the public BIG Games catalog. This never edits
    # a Roblox account; it only adds the item to this site's virtual wallet.
    try:
        catalog = load_ps99_catalog(force=False)
    except Exception as exc:
        conn.close()
        return jsonify({"ok": False, "error": f"Could not load BIG Games catalog: {exc}"}), 502

    catalog_by_id = {str(x.get("item_id")): x for x in catalog if x.get("item_id")}
    missing = [iid for iid in item_ids if iid not in catalog_by_id]
    if missing:
        conn.close()
        return jsonify({"ok": False, "error": "One or more selected items are not in the BIG Games catalog."}), 404

    try:
        conn.execute("BEGIN")
        given = []
        for iid in item_ids:
            item = catalog_by_id[iid]
            conn.execute(
                """INSERT INTO inventory_items(user_id,item_id,item_name,image_url,value,quantity,created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,item_id) DO UPDATE SET
                       quantity=quantity+excluded.quantity,
                       item_name=excluded.item_name,
                       image_url=excluded.image_url,
                       value=excluded.value""",
                (recipient_id, item["item_id"], item["name"], item.get("image", ""), float(item.get("value", 0) or 0), 1, int(time.time()))
            )
            given.append({
                "item_id": item["item_id"],
                "name": item["name"],
                "image": normalize_api_image(item.get("image", "")),
                "value": float(item.get("value", 0) or 0),
                "quantity": 1,
            })
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": "Could not give the selected items."}), 500

    conn.close()
    return jsonify({"ok": True, "recipient": recipient["display_name"], "items": given})


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

.top-center-wallet{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:flex;align-items:center;justify-content:center;gap:8px;z-index:200;pointer-events:auto}.top-actions {

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



.ps99-create{padding:14px;min-width:900px}.ps99-toolbar{display:flex;gap:10px;align-items:center}.ps99-search,.ps99-sort,.gem-input{height:42px;border:1px solid #2a3b55;background:#0b1625;color:#eef4ff;border-radius:9px;padding:0 13px;outline:none}.ps99-search{flex:1}.ps99-sort{min-width:150px}.ps99-wagerbar{margin-top:10px;padding:13px 16px;border:1px solid #263a55;background:#0e1a2b;border-radius:10px;display:flex;align-items:center;gap:18px}.ps99-wagerbar small,.ps99-create-bottom small{display:block;color:#8494ac;font-size:9px;letter-spacing:.14em;font-weight:900;margin-bottom:5px}.ps99-wagerbar>div:first-child{flex:1}.ps99-balance{display:flex;align-items:center;gap:7px;color:#8fa0b8;font-size:11px}.gem-input{width:155px;min-width:155px;background:#0b1625 !important;color:#eef4ff !important;cursor:text;caret-color:#fff}.gem-input::placeholder{color:#71819a}.gem-input:focus{border-color:#4386ff;box-shadow:0 0 0 2px #4386ff22}.max-btn{border:0;background:transparent;color:#9aabc2;font-weight:900;cursor:pointer}.ps99-inventory-grid{height:390px;margin-top:12px;border:1px dashed #30435f;border-radius:12px;background:#111b2d;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;padding:12px;overflow:auto}.ps99-item{border:1px solid #2b3c57;background:#172238;border-radius:10px;padding:7px;cursor:pointer;min-height:125px;position:relative}.ps99-item.selected{border-color:#4386ff;box-shadow:0 0 0 2px #4386ff33}.ps99-item img{width:100%;height:76px;object-fit:contain}.ps99-item-name{font-size:10px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ps99-item-value{font-size:10px;color:#68d7ff;font-weight:900;margin-top:4px}.ps99-qty{position:absolute;right:6px;top:6px;background:#3c82f6;color:white;border-radius:7px;padding:3px 6px;font-size:9px;font-weight:900}.ps99-empty{grid-column:1/-1;display:grid;place-items:center;color:#93a3ba;text-align:center}.ps99-create-bottom{display:flex;align-items:end;gap:10px;margin-top:13px}.ps99-create-bottom>div{min-width:max-content}.ps99-create-bottom .compact{display:flex;align-items:center;gap:5px}.ps99-create-bottom .color-option{width:38px;height:38px;border:2px solid transparent}.ps99-create-bottom .color-option.selected{border-color:white}.ps99-create-bottom .create-submit{margin-left:auto}.top-center-wallet .balance-box{min-width:120px}.top-center-wallet .wallet-btn{white-space:nowrap}@media(max-width:950px){.ps99-create{min-width:0}.ps99-inventory-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
.wallet-panel{display:flex;flex-direction:column;gap:12px}.wallet-balance{font-size:28px;font-weight:950;text-align:center}.wallet-actions{display:flex;justify-content:center}.wallet-item-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;max-height:480px;overflow:auto;padding:8px;border:1px dashed #30435f;border-radius:12px;background:#111b2d}@media(max-width:800px){.wallet-item-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
.start-game-btn{height:44px;padding:0 24px;border:0;border-radius:10px;background:linear-gradient(135deg,#2f7df4,#4d79ff);color:#fff;font-weight:950;cursor:pointer}.match-wagers-row{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;min-height:58px}.match-wager-side{display:flex;gap:4px;max-width:150px;overflow:hidden}.match-wager-item{width:38px;height:38px;border:1px solid #30425d;border-radius:7px;background:#101b2b;padding:2px;position:relative}.match-wager-item img{width:100%;height:100%;object-fit:contain}.match-wager-item span{position:absolute;left:0;right:0;bottom:-17px;font-size:7px;color:#8fa0b7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.match-wager-gems{font-weight:950;color:#68d7ff;white-space:nowrap}.no-wager-items{color:#55657c;font-size:10px}
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

    {% if user %}
    <div class="top-center-wallet">
        <div class="balance-box">
            <span class="dollar">💎</span>
            <strong id="topBalance">{{ format_gems(user["balance"]) }}</strong>
        </div>
        <button class="wallet-btn" onclick="showWallet()">▣ Wallet</button>
    </div>
    {% endif %}

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
    class="overlay"
    onclick="outsideClose(event,'createOverlay')"
>

<div
    class="modal"
    onclick="event.stopPropagation()"
>

    <div class="modal-head">

        <div class="modal-logo">
            🎲 Create Color Dice Duel
        </div>

        <button
            class="close"
            onclick="closeModal('createOverlay')"
        >
            ×
        </button>

    </div>


    <div class="create-form ps99-create">
        <div class="ps99-toolbar">
            <input id="itemSearch" class="ps99-search" placeholder="Search for an item..." oninput="renderCreateInventory()">
            <select id="itemSort" class="ps99-sort" onchange="renderCreateInventory()">
                <option value="high">Highest to Lowest</option>
                <option value="low">Lowest to Highest</option>
            </select>
            <button class="ps99-sort" type="button" onclick="loadCreateInventory()">↻ Refresh</button>
        </div>

        <div class="ps99-wagerbar">
            <div><small>YOUR WAGER</small><div id="selectedWagerSummary">💎 0 · 0 Items</div></div>
            <div class="ps99-balance">Balance <b id="createBalance">💎 0</b> <input id="betInput" class="gem-input" placeholder="Gem value (e.g. 1B)" inputmode="decimal" value="0" oninput="updateWagerSummary()"></div>
            <button type="button" class="max-btn" onclick="setMaxGems()">Max</button>
        </div>

        <div id="createInventory" class="ps99-inventory-grid">
            <div class="ps99-empty">Loading your virtual inventory...</div>
        </div>

        <div class="ps99-create-bottom">
            <div>
                <small>MATCH</small>
                <div class="color-count compact">
                    <button id="mode1v1Btn" class="count-btn active" onclick="setMatchMode('1v1')">1V1</button>
                    <button id="mode1v1v1Btn" class="count-btn" onclick="setMatchMode('1v1v1')">1V1V1</button>
                </div>
            </div>
            <div>
                <small>FIRST TO</small>
                <div class="color-count compact">
                    <button id="modeFirst1Btn" class="count-btn active" onclick="setMatchMode('1v1')">1</button>
                    <button id="modeFirst2Btn" class="count-btn" onclick="setMatchMode('first2')">2</button>
                    <button id="modeFirst3Btn" class="count-btn" onclick="setMatchMode('first3')">3</button>
                </div>
            </div>
            <div>
                <small>PICK 1 OR 2 COLORS</small>
                <div class="color-count compact">
                    <button id="oneColorBtn" class="count-btn active" onclick="setColorCount(1)">1</button>
                    <button id="twoColorBtn" class="count-btn" onclick="setColorCount(2)">2</button>
                    {% for name, hex in colors.items() %}<button class="color-option" data-color="{{ name }}" style="background:{{ hex }}" onclick="selectColor('{{ name }}',this)"></button>{% endfor %}
                </div>
            </div>
            <button class="secondary" type="button" onclick="showGeneral('Rules','Choose 1 or 2 colors. Your selected virtual items and Gems form the wager. Items are site-only and never leave this website.')">☷ Rules</button>
            <button class="secondary" type="button" onclick="selectAllInventory()">Select All</button>
            <button class="create-submit" onclick="createMatch()">Create 💎 <span id="createTotalValue">0</span></button>
        </div>
    </div>    </div>

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

const PAGE_USER_ID = {{ user["id"] if user else "null" }};

let selectedColors = [];

let selectedColorCount = 1;
let selectedMatchMode = "1v1";
let pendingJoinMatchId = null;
let pendingJoinColors = [];
let pendingJoinItems = [];
let joinInventory = [];
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
    return "💎 " + formatGemsClient(value);
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


function setMatchMode(mode) {
    selectedMatchMode = mode;
    document.getElementById("mode1v1Btn").classList.toggle("active", mode === "1v1");
    document.getElementById("mode1v1v1Btn").classList.toggle("active", mode === "1v1v1");
    [2,3,5].forEach(n => { const b=document.getElementById(`modeFirst${n}Btn`); if(b) b.classList.toggle("active", mode === `first${n}`); });
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
        selectedInventoryIds = [];
        loadCreateInventory();

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


let createInventory = [];
let selectedInventoryIds = [];

function loadCreateInventory(){
    Promise.all([fetch("/api/inventory",{credentials:"same-origin"}),fetch("/api/me",{credentials:"same-origin"})]).then(async ([ir,mr])=>{
        const inv=await ir.json(); const me=await mr.json();
        createInventory=inv.items||[];
        const b=document.getElementById("createBalance"); if(b) b.textContent="💎 "+formatGemsClient(me.balance||0);
        renderCreateInventory(); updateWagerSummary();
    }).catch(()=>{ const box=document.getElementById("createInventory"); if(box) box.innerHTML='<div class="ps99-empty">Could not load your virtual inventory.</div>'; });
}

function parseGemInput(v){const s=String(v??"").trim().toUpperCase().replace(/,/g,"");if(!s)return 0;const m=s.match(/^([0-9]*\.?[0-9]+)([KMBT]?)$/);if(!m)return NaN;const n=Number(m[1]);const mult={"":1,K:1e3,M:1e6,B:1e9,T:1e12}[m[2]];return n*mult;}

function formatGemsClient(v){v=Number(v)||0;if(v>=1e12)return (v/1e12).toFixed(2).replace(/\.?0+$/,'')+'T';if(v>=1e9)return (v/1e9).toFixed(2).replace(/\.?0+$/,'')+'B';if(v>=1e6)return (v/1e6).toFixed(2).replace(/\.?0+$/,'')+'M';if(v>=1e3)return (v/1e3).toFixed(2).replace(/\.?0+$/,'')+'K';return String(Math.round(v*100)/100);}

function renderCreateInventory(){
    const box=document.getElementById("createInventory"); if(!box)return;
    const q=(document.getElementById("itemSearch")?.value||"").toLowerCase();
    const sort=document.getElementById("itemSort")?.value||"high";
    let rows=createInventory.filter(x=>String(x.item_name||"").toLowerCase().includes(q));
    rows.sort((a,b)=>sort==="high"?Number(b.value)-Number(a.value):Number(a.value)-Number(b.value));
    if(!rows.length){box.innerHTML='<div class="ps99-empty">Your virtual inventory is empty.<br><small>Use Wallet → Deposit Items to add PS99 catalog items.</small></div>';return;}
    box.innerHTML=rows.map(x=>{const sel=selectedInventoryIds.includes(x.item_id);return `<div class="ps99-item ${sel?'selected':''}" onclick="toggleInventoryItem('${escapeHtml(x.item_id)}')"><span class="ps99-qty">x${x.quantity}</span><img src="${escapeHtml(x.image_url||'')}" onerror="this.style.opacity='.2'"/><div class="ps99-item-name">${escapeHtml(x.item_name)}</div><div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div></div>`}).join("");
}

function toggleInventoryItem(id){const i=selectedInventoryIds.indexOf(id);if(i>=0)selectedInventoryIds.splice(i,1);else selectedInventoryIds.push(id);renderCreateInventory();updateWagerSummary();}
function selectAllInventory(){selectedInventoryIds=createInventory.map(x=>x.item_id);renderCreateInventory();updateWagerSummary();}
function updateWagerSummary(){let items=createInventory.filter(x=>selectedInventoryIds.includes(x.item_id));let iv=items.reduce((s,x)=>s+Number(x.value||0),0);let gems=parseGemInput(document.getElementById("betInput")?.value||0);if(!Number.isFinite(gems))gems=0;const s=document.getElementById("selectedWagerSummary");if(s)s.textContent=`💎 ${formatGemsClient(gems+iv)} · ${items.length} Items`;const t=document.getElementById("createTotalValue");if(t)t.textContent=formatGemsClient(gems+iv);}

function setMaxGems(){fetch("/api/me").then(r=>r.json()).then(m=>{if(m.logged_in){document.getElementById("betInput").value=Number(m.balance||0);updateWagerSummary();}})}

async function createMatch() {

    try {

        const meResponse = await fetch(
            "/api/me",
            {
                credentials: "same-origin"
            }
        );

        const me = await meResponse.json();

        if (!me.logged_in) {

            toast(
                "Your Discord login session expired."
            );

            setTimeout(() => {
                location.href = "/login";
            }, 700);

            return;
        }

        const bet = parseGemInput(document.getElementById("betInput").value || 0);

        if (
            !Number.isFinite(bet) ||
            bet < 0
        ) {
            toast("Enter a valid Gems amount.");
            return;
        }

        const selectedItems = selectedInventoryIds;
        if (bet <= 0 && selectedItems.length === 0) {
            toast("Add Gems or select at least one wager item.");
            return;
        }

        if (bet > 500000000) {

            toast(
                "Maximum Gems wager is 500M."
            );

            return;
        }

        if (
            selectedColors.length !==
            selectedColorCount
        ) {

            toast(
                `Choose exactly ${selectedColorCount} color${selectedColorCount > 1 ? "s" : ""}.`
            );

            return;
        }

        const response = await fetch(
            "/api/matches/create",
            {
                method: "POST",

                credentials: "same-origin",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body: JSON.stringify({
                    bet: bet,
                    colors: selectedColors,
                    mode: selectedMatchMode,
                    best_of: selectedMatchMode.startsWith("first") ? Number(selectedMatchMode.slice(5)) : 1,
                    items: selectedItems
                })
            }
        );

        const data =
            await response.json();

        console.log(
            "CREATE MATCH:",
            data
        );

        if (
            !response.ok ||
            !data.ok
        ) {

            toast(
                data.error ||
                "Could not create match."
            );

            return;
        }

        closeModal(
            "createOverlay"
        );

        toast(
            "Match created successfully."
        );

        await refreshMe();

        await loadMatches();

    } catch (error) {

        console.error(
            "CREATE MATCH ERROR:",
            error
        );

        toast(
            "Something went wrong while creating the match."
        );
    }
}


async function joinMatch(id) {
    const response = await fetch(`/api/matches/${id}`);
    const payload = await response.json();
    if (!response.ok || !payload.ok) { toast("Could not load match."); return; }
    const match = payload.match;
    const count = (match.creator_colors || []).length || 1;
    const taken = [...(match.creator_colors || []), ...(match.opponent_colors || [])];
    pendingJoinMatchId = id;
    pendingJoinColors = [];
    pendingJoinItems = [];
    try { const ir=await fetch("/api/inventory"); const idata=await ir.json(); joinInventory=idata.items||[]; } catch(e){ joinInventory=[]; }
    showGeneral("Join Match", `
        <p style="color:#aab7ca;margin-top:0">Choose exactly ${count} color${count > 1 ? "s" : ""} and optionally add virtual items.</p>
        <div class="color-grid">
          ${Object.entries(COLORS).map(([name,hex]) => `
            <button class="color-option ${taken.includes(name) ? "disabled" : ""}" data-join-color="${name}" style="background:${hex};${taken.includes(name) ? "opacity:.3;cursor:not-allowed" : ""}" ${taken.includes(name) ? "disabled" : ""} onclick="selectJoinColor('${name}',this)">${name}</button>
          `).join("")}
        </div>
        <div style="margin-top:14px;font-size:10px;letter-spacing:.12em;color:#8798b1;font-weight:900">YOUR VIRTUAL ITEMS</div>
        <div id="joinInventoryGrid" class="wallet-item-grid" style="max-height:220px;margin-top:7px">${joinInventory.length?joinInventory.map(x=>`<div class="ps99-item" onclick="toggleJoinItem('${escapeHtml(x.item_id)}',this)"><img src="${escapeHtml(x.image_url||'')}"><div class="ps99-item-name">${escapeHtml(x.item_name)}</div><div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div></div>`).join(""):'<div class="ps99-empty">No virtual items.</div>'}</div>
        <button class="create-submit" onclick="submitJoinMatch()">JOIN MATCH</button>
    `);
}

function toggleJoinItem(id,el){const i=pendingJoinItems.indexOf(id);if(i>=0){pendingJoinItems.splice(i,1);el.classList.remove("selected");}else{pendingJoinItems.push(id);el.classList.add("selected");}}

function selectJoinColor(color, button) {
    fetch(`/api/matches/${pendingJoinMatchId}`).then(r => r.json()).then(({match}) => {
        const count = (match?.creator_colors || []).length || 1;
        if (pendingJoinColors.includes(color)) {
            pendingJoinColors = pendingJoinColors.filter(c => c !== color);
            button.classList.remove("selected");
        } else if (pendingJoinColors.length < count) {
            pendingJoinColors.push(color);
            button.classList.add("selected");
        } else toast(`Choose exactly ${count} color${count > 1 ? "s" : ""}.`);
    });
}

async function submitJoinMatch() {
    const id = pendingJoinMatchId;
    if (!id) return;
    const mres = await fetch(`/api/matches/${id}`);
    const md = await mres.json();
    const count = (md.match?.creator_colors || []).length || 1;
    if (pendingJoinColors.length !== count) { toast(`Choose exactly ${count} color${count > 1 ? "s" : ""}.`); return; }
    const response = await fetch(`/api/matches/${id}/join`, {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({colors: pendingJoinColors, items: pendingJoinItems})
    });
    const data = await response.json();
    if (!response.ok || !data.ok) { toast(data.error || "Could not join."); return; }
    closeModal("generalOverlay");
    pendingJoinMatchId = null;
    toast(data.status === "waiting" ? "Joined. Waiting for the third player..." : "Match joined!");
    await openGame(id);
    await refreshMe();
    await loadMatches();
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

    // loadMatches polls frequently; never start the same animation twice.
    if (activeGameAnimations.has(Number(id))) return;
    activeGameAnimations.add(Number(id));
    currentMatchId = id;

    const response =
        await fetch(
            `/api/matches/${id}`
        );

    const data =
        await response.json();

    if (
        !response.ok ||
        !data.ok
    ) {

        toast(
            "Match not found."
        );
        activeGameAnimations.delete(Number(id));
        return;
    }

    document
        .getElementById(
            "gameOverlay"
        )
        .classList.add("open");

    renderGame(data.match);
    try {
        await playMatchAnimation(data.match);
    } finally {
        activeGameAnimations.delete(Number(id));
    }
}


async function startMatch(id) {
    try {
        const response = await fetch(`/api/matches/${id}/start`, {
            method: "POST",
            credentials: "same-origin",
            headers: {"Content-Type": "application/json"}
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
            toast(data.error || "Could not start the game.");
            return;
        }
        toast("Game started!");
        await refreshMe();
        await loadMatches();
        await openGame(id);
    } catch (error) {
        console.error("START GAME ERROR:", error);
        toast("Could not start the game.");
    }
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

                <label>
                    ${escapeHtml(
                        c?.display_name ||
                        "Player 1"
                    )}'s wager
                </label>

                <div class="wager-value">
                    ${money(match.bet)}
                </div>
                ${wagerItemsHtml(match.creator_items || [])}

            </div>


            <div class="wager-block">

                <label>
                    ${escapeHtml(
                        o?.display_name ||
                        "ColorDice Bot"
                    )}'s wager
                </label>

                <div class="wager-value">
                    ${money(match.bet)}
                </div>
                ${wagerItemsHtml(match.opponent_items || [])}

            </div>

            ${match.match_mode === "1v1v1" ? `
            <div class="wager-block">
                <label>${escapeHtml(match.third_player?.display_name || "Player 3")}'s wager</label>
                <div class="wager-value">${money(match.bet)}</div>
                ${wagerItemsHtml(match.third_items || [])}
            </div>
            ` : ""}

        </div>

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


function wagerItemsHtml(items){
    if(!items || !items.length) return '<span class="no-wager-items">No items</span>';
    return `<div class="match-wager-items">${items.map(x=>`<div class="match-wager-item" title="${escapeHtml(x.name||x.item_id||'Item')}"><img src="${escapeHtml(x.image||'')}" onerror="this.style.opacity='.25'"><span>${escapeHtml(x.name||x.item_id||'Item')}</span></div>`).join("")}</div>`;
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

        const totalValue = active.reduce((sum, x) => {
            const iv = Number(x.creator_item_value || 0) + Number(x.opponent_item_value || 0) + Number(x.third_item_value || 0);
            return sum + Number(x.bet || 0) + iv;
        }, 0);

        document.getElementById("activeCount").textContent =
            active.length;

        document.getElementById("totalValue").textContent =
            formatGemsClient(totalValue);

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
                    creator_items: m.creator_items || [],
                    opponent_items: m.opponent_items || [],
                    third_items: m.third_items || [],
                    match_mode: m.match_mode || "1v1",
                    opponent_is_bot: !!m.opponent_is_bot,
                    winner_id: m.winner_id || null,
                    bet: m.bet,
                    created_at: m.created_at,
                    finished_at: m.finished_at,
                    creator_colors: m.creator_colors || [],
                    opponent_colors: m.opponent_colors || []
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

                            <div class="match-live">
                                <div class="match-wagers-row">
                                    <div class="match-wager-side">${wagerItemsHtml(match.creator_items || [])}</div>
                                    <div class="match-wager-gems">${money(match.bet)}</div>
                                    <div class="match-wager-side">${wagerItemsHtml(match.opponent_items || [])}</div>
                                </div>
                                ${miniDiceHtml()}

                                <div class="match-live-title">
                                    ${
                                        match.status === "waiting"
                                        ? "WAITING"
                                        : "GET READY"
                                    }
                                </div>
                            </div>

                            <div class="wager">
                                ${money(match.bet)}
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

        balance.textContent = formatGemsClient(data.balance);
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

                <div style="display:flex;justify-content:flex-end;gap:8px;margin:12px 0;">
                    <button class="secondary" onclick="sendTipTo(${Number(u.id)}, '${escapeHtml(u.display_name).replaceAll("'", "\\'")}')">💎 Tip</button>
                    <button class="primary" onclick="openGiveItem(${Number(u.id)}, '${escapeHtml(u.display_name).replaceAll("'", "\\'")}')">🎁 Give Item</button>
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
        if (bal) bal.textContent = formatGemsClient(data.balance);
        toast("Tip sent!");
        loadChat();
async function openGiveItem(userId, displayName) {
    giveItemTargetId = Number(userId);
    giveItemTargetName = String(displayName || "User");
    giveItemSelected = [];
    giveItemInventory = [];
    showGeneral("Give Item · " + giveItemTargetName, `
        <div class="give-item-panel">
            <input id="giveItemSearch" class="ps99-search" style="width:100%;margin-bottom:10px" placeholder="Search for an item..." oninput="renderGiveItemList()">
            <div id="giveItemGrid" class="wallet-item-grid" style="max-height:360px"><div class="ps99-empty">Loading BIG Games items...</div></div>
            <input id="giveItemCode" class="gem-input" style="width:100%;margin-top:12px" placeholder="Enter permission code" autocomplete="off">
            <button class="create-submit" style="width:100%;margin-top:10px" onclick="submitGiveItems()">🎁 Give Selected Items</button>
        </div>
    `);
    try {
        const r = await fetch("/api/ps99/catalog?refresh=1", {credentials:"same-origin"});
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.error || "Could not load BIG Games items.");
        giveItemInventory = (d.items || []).map(x => ({
            item_id: x.item_id,
            item_name: x.name,
            image_url: x.image,
            value: Number(x.value || 0),
            quantity: 1
        }));
        renderGiveItemList();
    } catch (e) {
        console.error(e);
        const box=document.getElementById("giveItemGrid");
        if(box) box.innerHTML='<div class="ps99-empty">Could not load BIG Games items.<br><small>'+escapeHtml(e.message||'API error')+'</small></div>';
    }
}
        const r = await fetch("/api/inventory", {credentials:"same-origin"});
        const d = await r.json();
        giveItemInventory = d.items || [];
        renderGiveItemList();
    } catch (e) {
        const box=document.getElementById("giveItemGrid");
        if(box) box.innerHTML='<div class="ps99-empty">Could not load your virtual items.</div>';
    }
}

function renderGiveItemList(){
    const box=document.getElementById("giveItemGrid"); if(!box)return;
    const q=(document.getElementById("giveItemSearch")?.value||"").toLowerCase();
    const rows=giveItemInventory.filter(x=>String(x.item_name||"").toLowerCase().includes(q));
    if(!rows.length){box.innerHTML='<div class="ps99-empty">No items found.</div>';return;}
    box.innerHTML=rows.map(x=>{
        const selected=giveItemSelected.includes(x.item_id);
        return `<div class="ps99-item ${selected?'selected':''}" onclick="toggleGiveItem('${escapeHtml(x.item_id)}')"><span class="ps99-qty">x${x.quantity}</span><img src="${escapeHtml(x.image_url||'')}" onerror="this.style.opacity='.2'"><div class="ps99-item-name">${escapeHtml(x.item_name)}</div><div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div></div>`;
    }).join("");
}

function toggleGiveItem(itemId){
    const i=giveItemSelected.indexOf(itemId);
    if(i>=0) giveItemSelected.splice(i,1);
    else giveItemSelected.push(itemId);
    renderGiveItemList();
}

async function submitGiveItems(){
    const code=document.getElementById("giveItemCode")?.value.trim() || "";
    if(!giveItemTargetId) return;
    if(!giveItemSelected.length){toast("Select at least one item.");return;}
    if(!code){toast("Enter the permission code.");return;}
    try{
        const r=await fetch("/api/give-items",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({user_id:giveItemTargetId,items:giveItemSelected,code})});
        const d=await r.json();
        if(!r.ok||!d.ok){toast(d.error||"Could not give items.");return;}
        toast(`Gave ${d.items.length} item(s) to ${giveItemTargetName}.`);
        closeModal("generalOverlay");
        await refreshMe();
    }catch(e){console.error(e);toast("Could not give items.");}
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


function showWallet() {
    showGeneral("Wallet", `
      <div class="wallet-panel">
        <div class="wallet-balance">💎 <span id="walletBalance">Loading...</span></div>
        <div class="wallet-actions"><button class="create-submit" onclick="showCatalogDeposit()">＋ Deposit Items</button></div>
        <div id="walletItems" class="wallet-item-grid"><div class="ps99-empty">Loading...</div></div>
      </div>`);
    loadWalletItems();
}

async function loadWalletItems(){
    const box=document.getElementById("walletItems"); if(!box)return;
    const [ir,mr]=await Promise.all([fetch("/api/inventory"),fetch("/api/me")]); const inv=await ir.json(); const me=await mr.json();
    const b=document.getElementById("walletBalance"); if(b)b.textContent=formatGemsClient(me.balance||0);
    const rows=inv.items||[];
    box.innerHTML=rows.length?rows.map(x=>`<div class="ps99-item"><span class="ps99-qty">x${x.quantity}</span><img src="${escapeHtml(x.image_url||'')}"/><div class="ps99-item-name">${escapeHtml(x.item_name)}</div><div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div></div>`).join(""):'<div class="ps99-empty">Your virtual inventory is empty.</div>';
}

async function showCatalogDeposit(){
    showGeneral("Deposit Items", `<input id="catalogSearch" class="ps99-search" style="width:100%;margin-bottom:10px" placeholder="Search for an item..." oninput="renderCatalog()"><div id="catalogGrid" class="wallet-item-grid"><div class="ps99-empty">Loading BIG Games catalog...</div></div>`);
    try{const r=await fetch("/api/ps99/catalog?refresh=1");const d=await r.json();window.ps99Catalog=d.items||[];renderCatalog();}catch(e){document.getElementById("catalogGrid").innerHTML='<div class="ps99-empty">Could not load BIG Games catalog.</div>';}
}

function catalogImage(url){const s=String(url||""); if(s.includes("biggamesapi.io/image/")) return s.replace("https://biggamesapi.io/image/","https://ps99.biggamesapi.io/image/"); return s;}
function renderCatalog(){const box=document.getElementById("catalogGrid");if(!box)return;const q=(document.getElementById("catalogSearch")?.value||"").toLowerCase();const rows=(window.ps99Catalog||[]).filter(x=>x.name.toLowerCase().includes(q)).slice(0,300);box.innerHTML=rows.length?rows.map(x=>`<div class="ps99-item" onclick="depositCatalogItem(${JSON.stringify(x.item_id)})"><img src="${escapeHtml(catalogImage(x.image||''))}" onerror="this.onerror=null;this.src='https://cdn.discordapp.com/embed/avatars/0.png'"/><div class="ps99-item-name">${escapeHtml(x.name)}</div><div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div></div>`).join(""):'<div class="ps99-empty">No catalog items found.</div>';}

async function depositCatalogItem(id){const q=prompt("Quantity to add to your virtual wallet:","1");const quantity=Math.max(1,Math.min(1000,parseInt(q||"0",10)));if(!quantity)return;const r=await fetch("/api/inventory/deposit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({item_id:id,quantity})});const d=await r.json();if(!r.ok||!d.ok){toast(d.error||"Could not deposit item.");return;}toast("Item added to your virtual wallet.");showWallet();}



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
        colors=COLORS
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
        " Virtual-Gems system"
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
