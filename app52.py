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
START_BALANCE = 10_000_000_000.00
MAX_BET = 5_000_000_000_000.00

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
        balance REAL NOT NULL DEFAULT 10000000000,
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
        opponent_bet REAL NOT NULL DEFAULT 0,
        third_bet REAL NOT NULL DEFAULT 0,

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
        third_items TEXT NOT NULL DEFAULT '[]',
        game_type TEXT NOT NULL DEFAULT 'colordice',
        creator_side TEXT,
        opponent_side TEXT,
        coin_result TEXT
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
        ("opponent_bet", "REAL NOT NULL DEFAULT 0"),
        ("third_bet", "REAL NOT NULL DEFAULT 0"),
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
        ("game_type", "TEXT NOT NULL DEFAULT 'colordice'"),
        ("creator_side", "TEXT"),
        ("opponent_side", "TEXT"),
        ("coin_result", "TEXT"),
    ):
        if _col not in match_columns:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {_col} {_definition}")

    # This site intentionally keeps only Huge / Titanic / Gargantuan virtual pets.
    # Remove older catalog entries that were imported before the PS99-only filter.
    conn.execute("""
        DELETE FROM inventory_items
        WHERE LOWER(item_name) NOT LIKE '%huge %'
          AND LOWER(item_name) NOT LIKE '%titanic %'
          AND LOWER(item_name) NOT LIKE '%gargantuan %'
    """)

    # Upgrade untouched legacy starter accounts from the old 1,000 Gems starter
    # balance to the new 10B starter balance. Accounts that have already played
    # are left untouched.
    conn.execute("""
        UPDATE users
        SET balance = ?
        WHERE balance = 1000
          AND games = 0
          AND total_wager = 0
          AND NOT EXISTS (
              SELECT 1 FROM inventory_items
              WHERE inventory_items.user_id = users.id
                AND inventory_items.quantity > 0
          )
    """, (START_BALANCE,))

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


def inventory_total_value(user_id):
    conn = db()
    row = conn.execute("SELECT COALESCE(SUM(value * quantity), 0) AS total FROM inventory_items WHERE user_id=? AND quantity>0", (user_id,)).fetchone()
    conn.close()
    return float(row["total"] or 0) if row else 0.0


def wallet_total_value(user_id, balance=None):
    if balance is None:
        conn = db()
        row = conn.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()
        conn.close()
        balance = float(row["balance"] or 0) if row else 0.0
    return float(balance or 0) + inventory_total_value(user_id)


app.jinja_env.globals["wallet_total_value"] = wallet_total_value


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

        "game_type": (
            "coinflip"
            if (
                ("creator_side" in row.keys() and row["creator_side"] in ("heads", "tails"))
                or ("opponent_side" in row.keys() and row["opponent_side"] in ("heads", "tails"))
                or ("coin_result" in row.keys() and row["coin_result"] in ("heads", "tails"))
                or ("game_type" in row.keys() and str(row["game_type"] or "").strip().lower() in ("coinflip", "coin-flip", "flip"))
            )
            else "colordice"
        ),

        "creator_side": row["creator_side"] if "creator_side" in row.keys() else None,
        "opponent_side": row["opponent_side"] if "opponent_side" in row.keys() else None,
        "coin_result": row["coin_result"] if "coin_result" in row.keys() else None,

        "best_of": int(row["best_of"] or 1) if "best_of" in row.keys() else 1,

        "series_wins": json.loads(row["series_wins"]) if "series_wins" in row.keys() and row["series_wins"] else {},

        "third_colors": parse_colors(row["third_color"]) if "third_color" in row.keys() else [],

        "bet": float(row["bet"]),
        # IMPORTANT: an empty opponent/third slot is NOT a wager.
        # Older code used `row["bet"]` as a fallback here, which made a brand-new
        # 1v1 match look like 3-way money and duplicated the creator wager.
        "creator_bet": float(row["bet"] or 0),
        "opponent_bet": float(row["opponent_bet"] or 0) if row["opponent_id"] or row["opponent_is_bot"] else 0.0,
        "third_bet": float(row["third_bet"] or 0) if row["third_player_id"] or row["third_is_bot"] else 0.0,

        "creator_items": normalize_wager_items(parse_item_list(row["creator_items"])) if "creator_items" in row.keys() else [],
        # Never copy creator_items into an empty opponent/third slot.
        "opponent_items": normalize_wager_items(parse_item_list(row["opponent_items"])) if (row["opponent_id"] or row["opponent_is_bot"]) and "opponent_items" in row.keys() else [],
        "third_items": normalize_wager_items(parse_item_list(row["third_items"])) if (row["third_player_id"] or row["third_is_bot"]) and "third_items" in row.keys() else [],
        "creator_item_value": item_total_value(parse_item_list(row["creator_items"])) if "creator_items" in row.keys() else 0,
        "opponent_item_value": item_total_value(parse_item_list(row["opponent_items"])) if (row["opponent_id"] or row["opponent_is_bot"]) and "opponent_items" in row.keys() else 0,
        "third_item_value": item_total_value(parse_item_list(row["third_items"])) if (row["third_player_id"] or row["third_is_bot"]) and "third_items" in row.keys() else 0,
        "creator_total_value": float(row["bet"] or 0) + (item_total_value(parse_item_list(row["creator_items"])) if "creator_items" in row.keys() else 0),
        "opponent_total_value": (float(row["opponent_bet"] or 0) if row["opponent_id"] or row["opponent_is_bot"] else 0.0) + (item_total_value(parse_item_list(row["opponent_items"])) if (row["opponent_id"] or row["opponent_is_bot"]) and "opponent_items" in row.keys() else 0),
        "third_total_value": (float(row["third_bet"] or 0) if row["third_player_id"] or row["third_is_bot"] else 0.0) + (item_total_value(parse_item_list(row["third_items"])) if (row["third_player_id"] or row["third_is_bot"]) and "third_items" in row.keys() else 0),

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

    wallet_value = wallet_total_value(user["id"], user["balance"])

    return jsonify({
        "logged_in": True,
        "id": user["id"],
        "username": user["username"],
        "display_name":
            user["display_name"],
        "avatar": user["avatar"],
        "balance":
            round(user["balance"], 2),
        "wallet_value":
            round(wallet_value, 2),
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
GIVE_ITEM_CODE = "523"


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


def _variant_from_flags(config_data):
    """Return the exact PS99 pet variant from BIG Games flags."""
    cfg = config_data if isinstance(config_data, dict) else {}
    shiny = bool(cfg.get("sh") in (1, True, "1", "true", "True"))
    try:
        pt = int(cfg.get("pt", 0) or 0)
    except Exception:
        pt = 0

    if shiny and pt == 1:
        return "Shiny Golden"
    if shiny and pt == 2:
        return "Shiny Rainbow"
    if pt == 1:
        return "Golden"
    if pt == 2:
        return "Rainbow"
    if shiny:
        return "Shiny"
    return "Regular"


def _variant_key(item_id, variant):
    """Stable virtual-inventory ID; keeps every PS99 variant separate."""
    return f"{item_id}::variant={variant.lower().replace(' ', '-') }"


def load_ps99_catalog(force=False):
    now = time.time()
    if not force and _PS99_CATALOG_CACHE["items"] and now < _PS99_CATALOG_CACHE["expires"]:
        return _PS99_CATALOG_CACHE["items"]

    # BIG Games' public legacy API is the authoritative source for PS99
    # collections, RAP and exists data.  The exists endpoint explicitly
    # exposes the pet variant flags: sh=Shiny, pt=1=Golden, pt=2=Rainbow.
    # We build one virtual catalog row per exact variant instead of reusing
    # the regular pet's RAP for every variant.
    pets = fetch_big_games_json("/api/collection/Pets")
    rap = fetch_big_games_json("/api/rap")
    exists = fetch_big_games_json("/api/exists")

    def rows_from(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return []

    def pet_key(item_id, cfg):
        try:
            pt = int(cfg.get("pt", 0) or 0)
        except Exception:
            pt = 0
        sh = 1 if cfg.get("sh") in (1, True, "1", "true", "True") else 0
        return (str(item_id).strip().lower(), sh, pt)

    # Exact RAP rows, including their sh/pt variant flags.
    rap_map = {}
    for row in rows_from(rap):
        if not isinstance(row, dict):
            continue
        cfg = row.get("configData") if isinstance(row.get("configData"), dict) else {}
        category = str(row.get("category") or cfg.get("category") or "")
        if category.lower() not in ("pet", "pets"):
            continue
        value = row.get("value")
        try:
            value = float(value or 0)
        except Exception:
            continue
        item_id = cfg.get("id") or row.get("configName") or row.get("id") or row.get("displayName")
        if item_id:
            rap_map[pet_key(item_id, cfg)] = value

    # Exact variant existence rows.  These are also what tells us which
    # variants actually exist in PS99, so we don't invent fake variants.
    exists_map = {}
    for row in rows_from(exists):
        if not isinstance(row, dict):
            continue
        category = str(row.get("category") or "")
        if category.lower() not in ("pet", "pets"):
            continue
        cfg = row.get("configData") if isinstance(row.get("configData"), dict) else {}
        item_id = cfg.get("id") or row.get("configName") or row.get("id")
        if not item_id:
            continue
        exists_map[pet_key(item_id, cfg)] = float(row.get("value") or 0)

    # Master pet metadata by base ID.
    pet_meta = {}
    for raw in rows_from(pets):
        if not isinstance(raw, dict):
            continue
        cfg = raw.get("configData") if isinstance(raw.get("configData"), dict) else {}
        item_id = str(raw.get("configName") or cfg.get("name") or raw.get("id") or cfg.get("id") or "").strip()
        if not item_id:
            continue
        pet_meta[item_id.lower()] = {
            "item_id": item_id,
            "name": str(cfg.get("name") or raw.get("displayName") or raw.get("configName") or item_id),
            "image": normalize_api_image(cfg.get("thumbnail") or raw.get("thumbnail") or cfg.get("icon") or raw.get("icon")),
            "golden_image": normalize_api_image(raw.get("goldenIcon") or cfg.get("goldenIcon") or ""),
            "category": str(raw.get("category") or cfg.get("category") or "Pets"),
        }

    result = []
    for (base_key, sh, pt), exists_value in exists_map.items():
        meta = pet_meta.get(base_key)
        if not meta:
            continue
        base_name_lower = str(meta.get("name", "")).lower()
        if not ("huge " in base_name_lower or "titanic " in base_name_lower or "gargantuan " in base_name_lower):
            continue

        if sh and pt == 1:
            variant = "Shiny Golden"
        elif sh and pt == 2:
            variant = "Shiny Rainbow"
        elif pt == 1:
            variant = "Golden"
        elif pt == 2:
            variant = "Rainbow"
        elif sh:
            variant = "Shiny"
        else:
            variant = "Regular"

        # Prefer exact RAP for this exact variant. Do not silently assign the
        # regular RAP to Golden/Rainbow/Shiny variants.
        value = rap_map.get((base_key, sh, pt), 0.0)

        image = meta["golden_image"] if pt == 1 and meta["golden_image"] else meta["image"]
        virtual_id = _variant_key(meta["item_id"], variant)
        display_name = meta["name"] if variant == "Regular" else f"{variant} {meta['name']}"

        result.append({
            "item_id": virtual_id,
            "base_item_id": meta["item_id"],
            "name": display_name,
            "base_name": meta["name"],
            "variant": variant,
            "shiny": bool(sh),
            "golden": pt == 1,
            "rainbow": pt == 2,
            "image": image,
            "value": float(value or 0),
            "exists": float(exists_value or 0),
            "category": meta["category"],
            "rap_exact": ((base_key, sh, pt) in rap_map),
        })

    # If an API installation has a pet in /collection/Pets but no /exists
    # row, keep one Regular entry so the catalog remains usable.
    represented = {(x["base_item_id"].lower(), x["variant"]) for x in result}
    for meta in pet_meta.values():
        base_name_lower = str(meta.get("name", "")).lower()
        if not ("huge " in base_name_lower or "titanic " in base_name_lower or "gargantuan " in base_name_lower):
            continue
        key = (meta["item_id"].lower(), "Regular")
        if key in represented:
            continue
        exact_rap = rap_map.get((meta["item_id"].lower(), 0, 0), 0.0)
        result.append({
            "item_id": _variant_key(meta["item_id"], "Regular"),
            "base_item_id": meta["item_id"],
            "name": meta["name"],
            "base_name": meta["name"],
            "variant": "Regular",
            "shiny": False,
            "golden": False,
            "rainbow": False,
            "image": meta["image"],
            "value": float(exact_rap or 0),
            "exists": 0,
            "category": meta["category"],
            "rap_exact": ((meta["item_id"].lower(), 0, 0) in rap_map),
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
# API - MATCH HISTORY
# ============================================================

@app.route("/api/history")
@login_required
def api_history():
    user = current_user()
    game_type = str(request.args.get("game_type", "all") or "all").strip().lower()
    if game_type not in ("all", "colordice", "coinflip"):
        game_type = "all"

    conn = db()
    rows = conn.execute(
        """SELECT * FROM matches
           WHERE status='finished'
             AND (creator_id=? OR opponent_id=? OR third_player_id=?)
           ORDER BY COALESCE(finished_at, created_at) DESC, id DESC
           LIMIT 100""",
        (user["id"], user["id"], user["id"])
    ).fetchall()
    conn.close()

    out=[]
    for row in rows:
        item=match_json(row)
        if game_type != "all" and item.get("game_type") != game_type:
            continue
        out.append(item)
    return jsonify(out)


# ============================================================
# API - MATCH LIST
# ============================================================

def row_is_coinflip(row):
    """Authoritative server-side game classifier.
    Explicit game_type wins; legacy side/result fields are only fallbacks.
    1v1v1 is always ColorDice.
    """
    mode = str(row["match_mode"] if "match_mode" in row.keys() else "1v1").strip().lower()
    if mode == "1v1v1":
        return False
    raw = str(row["game_type"] if "game_type" in row.keys() else "").strip().lower()
    if raw in ("coinflip", "coin-flip", "flip"):
        return True
    if raw == "colordice":
        return False
    for key in ("creator_side", "opponent_side", "coin_result"):
        if key in row.keys() and str(row[key] or "").strip().lower() in ("heads", "tails"):
            return True
    return False


@app.route("/api/matches")
def api_matches():

    conn = db()

    now = int(time.time())

    # Never expire a waiting match from the public lobby just because it is
    # older than three minutes. Only completed matches are time-limited.
    # This was the main cause of matches appearing to vanish from Coinflip.
    requested_game_type = str(request.args.get("game_type", "all") or "all").strip().lower()
    if requested_game_type not in ("all", "colordice", "coinflip"):
        requested_game_type = "all"

    # Filter on the server as well as the client. This prevents the two
    # Originals lobbies from depending on stale client-side game state.
    # Do not use a fragile server-side game filter here.
    # Older 1v1v1 rows can have legacy/null game_type values even though
    # they are valid ColorDice matches. Return the live match pool and let
    # the frontend classify the game reliably.
    where_game = ""
    query = f"""
        SELECT *
        FROM matches
        WHERE (
            status IN ('waiting', 'ready')
            OR (status = 'finished' AND COALESCE(finished_at, created_at) >= ?)
        )
        {where_game}
        ORDER BY
            CASE status
                WHEN 'waiting' THEN 0
                WHEN 'ready' THEN 1
                ELSE 2
            END,
            id DESC
        LIMIT 200
    """
    params = [now - 180]
    rows = conn.execute(query, params).fetchall()

    conn.close()

    # Apply the requested Original game on the SERVER as the final authority.
    # Never return a row belonging to the other Original. 1v1v1 is always
    # ColorDice. Legacy rows are classified only by row_is_coinflip().
    filtered_rows = []
    for row in rows:
        is_flip = row_is_coinflip(row)
        if requested_game_type == "coinflip" and not is_flip:
            continue
        if requested_game_type == "colordice" and is_flip:
            continue
        filtered_rows.append(row)

    response = jsonify([match_json(row) for row in filtered_rows])
    # Match state is live data. Never let the browser/proxy reuse an old
    # WAITING response after the bot/player has already joined.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
            "payout": float(row["bet"] or 0) + float(row["opponent_bet"] or 0) + float(row["third_bet"] or 0),
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

    game_type = str(data.get("game_type", "colordice")).lower()
    if game_type not in ("colordice", "coinflip"):
        game_type = "colordice"

    raw_mode = str(data.get("mode", "1v1"))
    match_mode = "1v1v1" if raw_mode == "1v1v1" and game_type == "colordice" else "1v1"
    try:
        best_of = int(data.get("best_of", 1))
    except (TypeError, ValueError):
        best_of = 1
    if best_of not in (1, 2, 3, 5):
        best_of = 1

    coin_side = str(data.get("side", "heads")).lower()
    if coin_side not in ("heads", "tails"):
        coin_side = "heads"

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

    if game_type == "colordice":
        if len(clean_colors) < 1:
            return jsonify({"ok": False, "error": "Choose at least one color."}), 400
        if len(clean_colors) > 2:
            return jsonify({"ok": False, "error": "You can choose up to 2 colors."}), 400
    else:
        clean_colors = []
        best_of = 1
        match_mode = "1v1"

    if bet < 0:
        return jsonify({"ok": False, "error": "Invalid Gems wager."}), 400

    if bet > MAX_BET:
        return jsonify({"ok": False, "error": "Maximum Gems wager is 5T (5000B)."}), 400

    conn = db()
    conn.execute("BEGIN IMMEDIATE")

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
            opponent_bet,
            third_bet,
            creator_color,
            status,
            created_at,
            opponent_is_bot,
            match_mode,
            best_of,
            creator_items,
            game_type,
            creator_side
        )
        VALUES (?, ?, ?, ?, ?, 'waiting', ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            user["id"],
            bet,
            0,
            0,
            save_colors(clean_colors),
            int(time.time()),
            match_mode,
            best_of,
            json.dumps(selected_items),
            game_type,
            coin_side if game_type == "coinflip" else None
        )
    )

    match_id = cursor.lastrowid

    for item in selected_items:
        conn.execute("UPDATE inventory_items SET quantity=quantity-1 WHERE user_id=? AND item_id=? AND quantity>0", (user["id"], item["item_id"]))

    conn.commit()
    conn.close()

    conn = db()
    created_row = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    created_payload = match_json(created_row) if created_row else None
    conn.close()

    return jsonify({
        "ok": True,
        "match_id": match_id,
        "game_type": game_type,
        "match": created_payload
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
    conn.execute("BEGIN IMMEDIATE")

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

    # Refund every real human who has already joined a still-waiting match.
    # This matters for 1v1v1: creator + human opponent can cancel while the
    # third slot is still empty, and the opponent must not lose their wager.
    refund_players = [
        (int(row["creator_id"]), float(row["bet"] or 0), parse_item_list(row["creator_items"])),
    ]
    if row["opponent_id"] and not row["opponent_is_bot"]:
        refund_players.append((int(row["opponent_id"]), float(row["opponent_bet"] or 0), parse_item_list(row["opponent_items"])))
    if row["third_player_id"] and not row["third_is_bot"]:
        refund_players.append((int(row["third_player_id"]), float(row["third_bet"] or 0), parse_item_list(row["third_items"])))

    for refund_user_id, refund_gems, refund_items in refund_players:
        for item in refund_items:
            iid = str(item.get("item_id", ""))
            if not iid:
                continue
            conn.execute("""INSERT INTO inventory_items(user_id,item_id,item_name,image_url,value,quantity,created_at)
                            VALUES(?,?,?,?,?,?,?)
                            ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity, item_name=excluded.item_name, image_url=excluded.image_url, value=excluded.value""",
                         (refund_user_id, iid, item.get("name", iid), item.get("image", ""), float(item.get("value", 0) or 0), int(item.get("quantity", 1) or 1), int(time.time())))
        conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (refund_gems, refund_user_id))

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
    conn.execute("BEGIN IMMEDIATE")

    row = conn.execute(
        "SELECT * FROM matches WHERE id = ?",
        (match_id,)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Match not found."}), 404

    is_three = row["match_mode"] == "1v1v1"
    waiting_for_third = is_three and row["opponent_id"] and not row["third_player_id"]

    if row["status"] != "waiting" or (is_three and row["opponent_id"] and row["third_player_id"]):
        conn.close()
        return jsonify({"ok": False, "error": "This match is no longer waiting."}), 400

    if row["creator_id"] == user["id"] or row["opponent_id"] == user["id"] or row["third_player_id"] == user["id"]:
        conn.close()
        return jsonify({"ok": False, "error": "You cannot join your own match."}), 400

    active = conn.execute(
        """
        SELECT id FROM matches
        WHERE (creator_id = ? OR opponent_id = ? OR third_player_id = ?)
          AND status IN ('waiting', 'ready')
        LIMIT 1
        """,
        (user["id"], user["id"], user["id"])
    ).fetchone()

    if active:
        conn.close()
        return jsonify({"ok": False, "error": "You already have an active match."}), 400

    data = request.get_json(silent=True) or {}

    try:
        join_gems = float(data.get("bet", 0) or 0)
    except (TypeError, ValueError):
        join_gems = -1

    if join_gems < 0 or join_gems > MAX_BET:
        conn.close()
        return jsonify({"ok": False, "error": "Invalid Gems wager."}), 400

    wager_items = data.get("items", [])
    if not isinstance(wager_items, list):
        wager_items = []

    wager_item_ids = list(dict.fromkeys(
        str(x).strip() for x in wager_items if str(x).strip()
    ))[:100]

    selected_items = []
    if wager_item_ids:
        placeholders = ",".join("?" for _ in wager_item_ids)
        item_rows = conn.execute(
            f"""
            SELECT * FROM inventory_items
            WHERE user_id = ?
            AND item_id IN ({placeholders})
            AND quantity > 0
            """,
            [user["id"], *wager_item_ids]
        ).fetchall()
        by_id = {r["item_id"]: r for r in item_rows}
        for iid in wager_item_ids:
            r = by_id.get(iid)
            if not r:
                conn.close()
                return jsonify({"ok": False, "error": "One or more wager items are not in your virtual wallet."}), 400
            selected_items.append({
                "item_id": r["item_id"],
                "name": r["item_name"],
                "image": r["image_url"],
                "value": float(r["value"]),
                "quantity": 1,
            })

    creator_items = parse_item_list(row["creator_items"]) if "creator_items" in row.keys() else []
    creator_total_value = float(row["bet"] or 0) + item_total_value(creator_items)
    min_join_total = creator_total_value * 0.90
    max_join_total = creator_total_value * 1.10
    join_item_value = item_total_value(selected_items)
    join_total_value = join_gems + join_item_value

    if creator_total_value <= 0:
        conn.close()
        return jsonify({"ok": False, "error": "This match has an invalid wager."}), 400

    if join_total_value < min_join_total - 0.01 or join_total_value > max_join_total + 0.01:
        conn.close()
        return jsonify({
            "ok": False,
            "error": f"Your total wager must be between {format_gems(min_join_total)} and {format_gems(max_join_total)} Gems."
        }), 400

    fresh = conn.execute(
        "SELECT balance FROM users WHERE id = ?",
        (user["id"],)
    ).fetchone()

    if not fresh or float(fresh["balance"] or 0) < join_gems:
        conn.close()
        return jsonify({"ok": False, "error": "Insufficient Gems balance."}), 400

    creator_colors = parse_colors(row["creator_color"])
    chosen = data.get("colors", [])
    if not isinstance(chosen, list):
        chosen = [chosen]
    chosen = list(dict.fromkeys(
        str(c).lower() for c in chosen if str(c).lower() in COLORS
    ))

    if row["game_type"] == "coinflip":
        chosen_side = str(data.get("side", "heads")).lower()
        if chosen_side not in ("heads", "tails"):
            conn.close(); return jsonify({"ok": False, "error": "Choose Heads or Tails."}), 400
        if chosen_side == str(row["creator_side"] or "heads"):
            conn.close(); return jsonify({"ok": False, "error": "That side is already taken."}), 400
    else:
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
        (join_gems, join_gems, user["id"])
    )

    for item in selected_items:
        conn.execute(
            "UPDATE inventory_items SET quantity=quantity-1 WHERE user_id=? AND item_id=? AND quantity>0",
            (user["id"], item["item_id"])
        )

    if row["game_type"] == "coinflip":
        opponent_side = "tails" if str(row["creator_side"] or "heads") == "heads" else "heads"
        conn.execute(
            """UPDATE matches SET opponent_id=?, opponent_side=?, opponent_items=?, opponent_bet=?, opponent_is_bot=0, status='ready' WHERE id=?""",
            (user["id"], opponent_side, json.dumps(selected_items), join_gems, match_id)
        )
        new_status = "ready"
    elif waiting_for_third:
        conn.execute(
            """
            UPDATE matches
            SET third_player_id = ?,
                third_color = ?,
                third_items = ?,
                third_bet = ?,
                third_is_bot = 0,
                status = 'ready'
            WHERE id = ?
            """,
            (user["id"], save_colors(chosen), json.dumps(selected_items), join_gems, match_id)
        )
        new_status = "ready"
    else:
        conn.execute(
            """
            UPDATE matches
            SET opponent_id = ?,
                opponent_color = ?,
                opponent_items = ?,
                opponent_bet = ?,
                opponent_is_bot = 0,
                status = ?
            WHERE id = ?
            """,
            (user["id"], save_colors(chosen), json.dumps(selected_items), join_gems,
             "ready" if not is_three else "waiting", match_id)
        )
        new_status = "ready" if not is_three else "waiting"

    conn.commit()
    conn.close()

    final_match = settle_match(match_id) if new_status == "ready" else None

    conn2 = db()
    final = conn2.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    conn2.close()

    return jsonify({
        "ok": True,
        "match_id": match_id,
        "status": final["status"] if final else new_status,
        "match": match_json(final),
        "started": bool(final_match),
    })


@app.route(
    "/api/matches/<int:match_id>/call-bot",
    methods=["POST"]
)
@login_required
def call_bot(match_id):
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Login with Discord first."}), 401

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Match not found."}), 404

    if int(row["creator_id"]) != int(user["id"]):
        conn.close()
        return jsonify({"ok": False, "error": "Only the creator can call the bot."}), 403

    if row["status"] != "waiting":
        conn.close()
        return jsonify({"ok": False, "error": "This match is no longer waiting."}), 400

    # --------------------------------------------------------
    # COINFLIP: exactly one bot fills the opponent slot.
    # --------------------------------------------------------
    if row["game_type"] == "coinflip":
        bot_side = "tails" if str(row["creator_side"] or "heads") == "heads" else "heads"
        bot_gems = float(row["bet"] or 0)
        conn.execute(
            """UPDATE matches
               SET opponent_id=NULL, opponent_side=?, opponent_bet=?,
                   opponent_items='[]', opponent_is_bot=1, status='ready'
               WHERE id=?""",
            (bot_side, bot_gems, match_id)
        )
        conn.commit()
        conn.close()
        final_match = settle_match(match_id)
        if not final_match:
            return jsonify({"ok": False, "error": "Could not start the bot coinflip."}), 500
        return jsonify({
            "ok": True, "match_id": match_id, "bot_side": bot_side,
            "status": "finished", "started": True
        })

    creator_colors = parse_colors(row["creator_color"])
    if not creator_colors:
        conn.close()
        return jsonify({"ok": False, "error": "Creator colors are missing."}), 400

    # --------------------------------------------------------
    # Build a bot wager. This is used for the single bot slot
    # that is actually being filled. Existing human wagers are
    # NEVER overwritten.
    # --------------------------------------------------------
    creator_items = parse_item_list(row["creator_items"]) if "creator_items" in row.keys() else []
    creator_total = float(row["bet"] or 0) + item_total_value(creator_items)

    def make_bot_wager(target_total):
        target_total = max(0.0, float(target_total or 0))
        desired = target_total * random.uniform(0.90, 1.10)
        try:
            catalog = [x for x in load_ps99_catalog() if float(x.get("value", 0) or 0) > 0]
        except Exception:
            catalog = []

        def kind(x):
            n = str(x.get("name", "")).lower()
            if "gargantuan" in n: return "gargantuan"
            if "titanic" in n: return "titanic"
            if "huge" in n: return "huge"
            return "other"

        catalog = [x for x in catalog if kind(x) in ("huge", "titanic", "gargantuan")]
        tit = [x for x in catalog if kind(x) == "titanic"]
        garg = [x for x in catalog if kind(x) == "gargantuan"]
        huge = [x for x in catalog if kind(x) == "huge"]
        chosen = []

        # 3B~15B: Titanic-only bot wager. Prefer a single Titanic near the
        # target, then add another Titanic if the catalog needs it. No Huge/Garg.
        if 3_000_000_000 <= desired < 15_000_000_000 and tit:
            pool = [x for x in tit if float(x.get("value", 0)) <= desired * 1.02]
            if pool:
                first = min(pool, key=lambda x: abs(float(x["value"]) - desired * random.uniform(.78, .98)))
                chosen.append(first)
                total = float(first.get("value", 0) or 0)
                remaining = desired - total
                if remaining > 0.65 * desired:
                    extra_pool = [x for x in tit if float(x.get("value", 0)) <= remaining]
                    if extra_pool:
                        chosen.append(min(extra_pool, key=lambda x: abs(float(x["value"]) - remaining)))

        # 15B~100B: Titanic-heavy only. Multiple Titanics are allowed; this
        # avoids suddenly dropping a Huge into a large match.
        if not chosen and 15_000_000_000 <= desired < 100_000_000_000 and tit:
            remaining = desired
            ranked = sorted(tit, key=lambda x: float(x.get("value", 0) or 0), reverse=True)
            for _ in range(8):
                pool = [x for x in ranked if float(x.get("value", 0) or 0) <= remaining]
                if not pool: break
                pick = pool[min(len(pool)-1, random.randrange(min(3, len(pool))))]
                chosen.append(pick)
                remaining -= float(pick.get("value", 0) or 0)
                if remaining <= 0.10 * desired: break

        # 100B+: one large Gargantuan in the 100~200B range when available,
        # followed by several smaller Titanics to make up the rest.
        if not chosen and desired >= 100_000_000_000:
            garg_pool = [x for x in garg if 100_000_000_000 <= float(x.get("value", 0) or 0) <= 200_000_000_000]
            if garg_pool:
                g = random.choice(garg_pool)
                chosen.append(g)
                remaining = max(0.0, desired - float(g.get("value", 0) or 0))
                small_tit = sorted(
                    [x for x in tit if float(x.get("value", 0) or 0) <= remaining],
                    key=lambda x: float(x.get("value", 0) or 0)
                )
                # Fill with several smaller Titanics, not one giant Titanic.
                while remaining > 5_000_000_000 and small_tit and len(chosen) < 12:
                    pool = [x for x in small_tit if float(x.get("value", 0) or 0) <= remaining]
                    if not pool: break
                    # Prefer values that use roughly 8~22% of the remaining pot.
                    pick = min(pool, key=lambda x: abs(float(x.get("value", 0) or 0) - remaining * random.uniform(.08, .22)))
                    chosen.append(pick)
                    remaining -= float(pick.get("value", 0) or 0)

        # Fallback: keep the wager item-only when a suitable catalog item exists.
        if not chosen:
            eligible = [x for x in catalog if float(x.get("value", 0) or 0) <= desired]
            if eligible:
                preferred = [x for x in eligible if desired * 0.55 <= float(x.get("value", 0)) <= desired * 0.95]
                chosen = [min(preferred or eligible, key=lambda x: abs(float(x["value"]) - desired * .75))]

        chosen = [
            {
                "item_id": x["item_id"], "name": x["name"],
                "image": x.get("image", ""), "value": float(x.get("value", 0) or 0),
                "quantity": 1, "variant": x.get("variant", "Regular")
            }
            for x in chosen
        ]
        item_value = item_total_value(chosen)
        bot_gems = max(0.0, desired - item_value)
        # For the requested large-match behavior, items are the visible wager.
        # Gems only fill a tiny remainder if the catalog cannot hit the target.
        final_total = item_value + bot_gems
        if target_total > 0 and not (target_total * 0.90 <= final_total <= target_total * 1.10):
            bot_gems = 0.0
        return bot_gems, chosen

    # --------------------------------------------------------
    # 1V1V1 CRITICAL FIX
    # - Never replace an existing human opponent.
    # - If creator + human opponent are present, the bot fills
    #   ONLY the third slot.
    # - If creator is alone, the bot fills ONLY the opponent slot
    #   and the match remains waiting for the third player.
    # - If a third human is already present, do nothing/return.
    # --------------------------------------------------------
    if row["match_mode"] == "1v1v1":
        human_opponent = bool(row["opponent_id"] and int(row["opponent_id"]) > 0)
        human_third = bool(row["third_player_id"] and int(row["third_player_id"]) > 0)
        opponent_bot = bool(row["opponent_is_bot"])
        third_bot = bool(row["third_is_bot"])

        if human_third or third_bot:
            conn.close()
            return jsonify({"ok": False, "error": "This 1v1v1 match already has three players."}), 400

        available = [c for c in COLOR_NAMES if c not in creator_colors]
        if human_opponent:
            opponent_colors = parse_colors(row["opponent_color"])
            available = [c for c in available if c not in opponent_colors]
            if not available:
                conn.close()
                return jsonify({"ok": False, "error": "No color is available for the bot."}), 400

            bot_count = min(len(creator_colors), len(available))
            bot_colors = random.sample(available, bot_count)
            bot_gems, bot_items = make_bot_wager(creator_total)

            conn.execute(
                """UPDATE matches
                   SET third_player_id=NULL,
                       third_color=?,
                       third_bet=?,
                       third_items=?,
                       third_is_bot=1,
                       status='ready'
                   WHERE id=?""",
                (save_colors(bot_colors), bot_gems, json.dumps(bot_items), match_id)
            )
            filled_slot = "third"
        elif not opponent_bot:
            if not available:
                conn.close()
                return jsonify({"ok": False, "error": "No color is available for the bot."}), 400

            bot_count = min(len(creator_colors), len(available))
            bot_colors = random.sample(available, bot_count)
            bot_gems, bot_items = make_bot_wager(creator_total)

            conn.execute(
                """UPDATE matches
                   SET opponent_id=NULL,
                       opponent_color=?,
                       opponent_bet=?,
                       opponent_items=?,
                       opponent_is_bot=1,
                       status='waiting'
                   WHERE id=?""",
                (save_colors(bot_colors), bot_gems, json.dumps(bot_items), match_id)
            )
            filled_slot = "opponent"
        else:
            conn.close()
            return jsonify({"ok": False, "error": "A bot is already in this match."}), 400

        conn.commit()
        conn.close()

        # Only start when all three slots are occupied. If creator + bot
        # are the only players, the match correctly stays WAITING.
        check = db()
        latest = check.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        check.close()
        full = bool(
            latest and
            (latest["opponent_id"] or latest["opponent_is_bot"]) and
            (latest["third_player_id"] or latest["third_is_bot"])
        )
        if full:
            final_match = settle_match(match_id)
            if not final_match:
                return jsonify({"ok": False, "error": "Could not start the 1v1v1 bot match."}), 500
            return jsonify({
                "ok": True, "match_id": match_id, "status": "finished",
                "started": True, "filled_slot": filled_slot,
                "bot_colors": bot_colors
            })

        return jsonify({
            "ok": True, "match_id": match_id, "status": "waiting",
            "started": False, "filled_slot": filled_slot,
            "bot_colors": bot_colors
        })

    # --------------------------------------------------------
    # Normal 1V1 ColorDice: one bot fills the opponent slot.
    # --------------------------------------------------------
    available_colors = [color for color in COLOR_NAMES if color not in creator_colors]
    if not available_colors:
        conn.close()
        return jsonify({"ok": False, "error": "There are no colors available for the bot."}), 400

    bot_color_count = min(len(creator_colors), len(available_colors))
    bot_colors = random.sample(available_colors, bot_color_count)
    bot_gems, bot_items = make_bot_wager(creator_total)

    conn.execute(
        """UPDATE matches
           SET opponent_id=NULL, opponent_color=?, opponent_bet=?,
               opponent_items=?, opponent_is_bot=1, status='ready'
           WHERE id=?""",
        (save_colors(bot_colors), bot_gems, json.dumps(bot_items), match_id)
    )
    conn.commit()
    conn.close()

    final_match = settle_match(match_id)
    if not final_match:
        return jsonify({"ok": False, "error": "Could not start the bot match."}), 500

    return jsonify({
        "ok": True, "match_id": match_id, "bot_colors": bot_colors,
        "status": "finished", "started": True
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
    conn.execute("BEGIN IMMEDIATE")

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

    if row["game_type"] == "coinflip":
        result = random.choice(("heads", "tails"))
        creator_side = str(row["creator_side"] or "heads")
        winner = creator if result == creator_side else (opponent if opponent else -1)
        pot = float(row["bet"] or 0) + float(row["opponent_bet"] or 0)
        if winner > 0:
            conn.execute("UPDATE users SET balance=balance+?, wins=wins+1, games=games+1 WHERE id=?", (pot, winner))
        loser = opponent if winner == creator else creator
        if loser and loser > 0:
            conn.execute("UPDATE users SET losses=losses+1, games=games+1 WHERE id=?", (loser,))

        # Transfer site-only virtual wager items to the winner.
        if winner > 0:
            for col in ("creator_items", "opponent_items"):
                for item in parse_item_list(row[col] if col in row.keys() else "[]"):
                    iid = str(item.get("item_id", ""))
                    qty = int(item.get("quantity", 1) or 1)
                    if not iid or qty < 1: continue
                    conn.execute("""INSERT INTO inventory_items(user_id,item_id,item_name,image_url,value,quantity,created_at)
                                    VALUES(?,?,?,?,?,?,?)
                                    ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity, item_name=excluded.item_name, image_url=excluded.image_url, value=excluded.value""",
                                 (winner, iid, item.get("name", iid), item.get("image", ""), float(item.get("value", 0) or 0), qty, int(time.time())))

        animation_started_at = int(time.time() * 1000)
        conn.execute("UPDATE matches SET status='finished', winner_id=?, coin_result=?, rolls=?, finished_at=?, animation_started_at=? WHERE id=?",
                     (winner, result, json.dumps([[result]]), int(time.time()), animation_started_at, match_id))
        conn.commit()
        final = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        conn.close()
        return final

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
        final_three_scores = {str(creator): 0, str(opponent if opponent else -1): 0, str(row["third_player_id"] if row["third_player_id"] else -2): 0}
        for _ in range(100):
            rolls = roll_round()
            roll_history.append(rolls)
            scores = {i: color_score(rolls, players[i]["colors"]) for i in alive}
            final_three_scores = {str(players[i]["id"]): int(scores[i]) for i in alive}
            high = max(scores.values())
            survivors = [i for i in alive if scores[i] == high]
            # A unique highest scorer wins; otherwise only the tied leaders reroll.
            if len(survivors) == 1:
                winner = players[survivors[0]]["id"]
                break
            alive = survivors
        if winner is None:
            winner = players[alive[0]]["id"]
        creator_gems = float(row["bet"] or 0)
        opponent_gems = float(row["opponent_bet"] or 0)
        third_gems = float(row["third_bet"] or 0)
        winner_is_human = winner > 0
        if winner_is_human:
            conn.execute("UPDATE users SET balance = balance + ?, wins = wins + 1, games = games + 1 WHERE id = ?", (creator_gems + opponent_gems + third_gems, winner))
        for p in players:
            if p["id"] > 0 and p["id"] != winner:
                conn.execute("UPDATE users SET losses = losses + 1, games = games + 1 WHERE id = ?", (p["id"],))
        # Transfer ALL virtual wager items from every participant to the winner.
        # This is intentionally separate from the Gems payout because item
        # value must never be minted as Gems.
        if winner > 0:
            for col in ("creator_items", "opponent_items", "third_items"):
                if col not in row.keys():
                    continue
                for item in parse_item_list(row[col]):
                    iid = str(item.get("item_id", ""))
                    qty = int(item.get("quantity", 1) or 1)
                    if not iid or qty < 1:
                        continue
                    conn.execute(
                        """INSERT INTO inventory_items(user_id,item_id,item_name,image_url,value,quantity,created_at)
                           VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(user_id,item_id) DO UPDATE SET
                               quantity=quantity+excluded.quantity,
                               item_name=excluded.item_name,
                               image_url=excluded.image_url,
                               value=excluded.value""",
                        (winner, iid, item.get("name", iid), item.get("image", ""),
                         float(item.get("value", 0) or 0), qty, int(time.time()))
                    )

        animation_started_at = int(time.time() * 1000)
        conn.execute(
            "UPDATE matches SET status='finished', winner_id=?, rolls=?, series_wins=?, finished_at=?, animation_started_at=? WHERE id=?",
            (winner, json.dumps(roll_history), json.dumps({"target": 1, "scores": final_three_scores, "results": []}), int(time.time()), animation_started_at, match_id)
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

        pot = float(row["bet"] or 0) + float(row["opponent_bet"] or 0)

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

            payout = float(row["bet"] or 0) + float(row["opponent_bet"] or 0)

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

    response = jsonify({
        "ok": True,
        "match": match_json(row)
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
# TIP TRANSFER (Gems + virtual inventory items)
# ============================================================
@app.route("/api/tip", methods=["POST"])
@login_required
def api_tip():
    sender = current_user()
    data = request.get_json(silent=True) or {}

    try:
        recipient_id = int(data.get("user_id"))
        amount = round(float(data.get("amount", 0) or 0), 2)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid recipient or Gems amount."}), 400

    if recipient_id == sender["id"]:
        return jsonify({"ok": False, "error": "You cannot tip yourself."}), 400

    if amount < 0 or amount > 1000000:
        return jsonify({"ok": False, "error": "Gems tip must be between 0 and 1M Gems."}), 400

    item_ids = data.get("items", [])
    if not isinstance(item_ids, list):
        item_ids = [item_ids]
    item_ids = list(dict.fromkeys(str(x).strip() for x in item_ids if str(x).strip()))
    if len(item_ids) > 100:
        return jsonify({"ok": False, "error": "You can tip up to 100 items at once."}), 400

    if amount <= 0 and not item_ids:
        return jsonify({"ok": False, "error": "Select at least one item or enter a Gems amount."}), 400

    conn = db()
    recipient = conn.execute("SELECT id, display_name FROM users WHERE id=?", (recipient_id,)).fetchone()
    if not recipient:
        conn.close()
        return jsonify({"ok": False, "error": "User not found."}), 404

    sender_balance = conn.execute("SELECT balance FROM users WHERE id=?", (sender["id"],)).fetchone()
    if not sender_balance or float(sender_balance["balance"] or 0) < amount:
        conn.close()
        return jsonify({"ok": False, "error": "Insufficient Gems balance."}), 400

    selected_rows = []
    if item_ids:
        placeholders = ",".join("?" for _ in item_ids)
        rows = conn.execute(
            f"SELECT * FROM inventory_items WHERE user_id=? AND item_id IN ({placeholders}) AND quantity>0",
            [sender["id"], *item_ids]
        ).fetchall()
        by_id = {str(r["item_id"]): r for r in rows}
        missing = [iid for iid in item_ids if iid not in by_id]
        if missing:
            conn.close()
            return jsonify({"ok": False, "error": "One or more selected items are not in your virtual wallet."}), 400
        selected_rows = [by_id[iid] for iid in item_ids]

    try:
        conn.execute("BEGIN")

        if amount > 0:
            conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (amount, sender["id"]))
            conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, recipient_id))

        given_items = []
        for row in selected_rows:
            # One unit of each selected virtual item is transferred.
            conn.execute(
                "UPDATE inventory_items SET quantity=quantity-1 WHERE user_id=? AND item_id=? AND quantity>0",
                (sender["id"], row["item_id"])
            )
            conn.execute(
                """INSERT INTO inventory_items(user_id,item_id,item_name,image_url,value,quantity,created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,item_id) DO UPDATE SET
                       quantity=quantity+excluded.quantity,
                       item_name=excluded.item_name,
                       image_url=excluded.image_url,
                       value=excluded.value""",
                (
                    recipient_id,
                    row["item_id"],
                    row["item_name"],
                    row["image_url"],
                    float(row["value"] or 0),
                    1,
                    int(time.time()),
                )
            )
            given_items.append({
                "item_id": row["item_id"],
                "name": row["item_name"],
                "image": normalize_api_image(row["image_url"]),
                "value": float(row["value"] or 0),
                "quantity": 1,
            })

        sender_name = conn.execute(
            "SELECT display_name FROM users WHERE id=?",
            (sender["id"],)
        ).fetchone()["display_name"]

        parts = []
        if amount > 0:
            parts.append(f"{format_gems(amount)} Gems")
        if given_items:
            parts.append(f"{len(given_items)} item(s)")
        public_message = f"🎁 {sender_name} sent {recipient['display_name']} " + " + ".join(parts) + "!"
        conn.execute(
            "INSERT INTO chat(user_id,message,created_at) VALUES(?,?,?)",
            (sender["id"], public_message, int(time.time()))
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": "Could not complete the tip."}), 500

    new_balance = float(sender_balance["balance"] or 0) - amount
    conn.close()
    return jsonify({
        "ok": True,
        "message": public_message,
        "balance": new_balance,
        "items": given_items,
        "amount": amount,
    })


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

    item_ids = data.get("items", [])
    if not isinstance(item_ids, list):
        item_ids = [item_ids]
    item_ids = list(dict.fromkeys(str(x).strip() for x in item_ids if str(x).strip()))

    try:
        give_gems = float(data.get("gems", 0) or 0)
    except (TypeError, ValueError):
        give_gems = 0.0
    if not (give_gems >= 0):
        return jsonify({"ok": False, "error": "Invalid Gems amount."}), 400
    if give_gems > MAX_BET:
        return jsonify({"ok": False, "error": "Gems amount is too large."}), 400
    if not item_ids and give_gems <= 0:
        return jsonify({"ok": False, "error": "Select an item or enter a Gems amount."}), 400
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
        if give_gems > 0:
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE id = ?",
                (give_gems, recipient_id)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": "Could not give the selected items."}), 500

    conn.close()
    return jsonify({"ok": True, "recipient": recipient["display_name"], "items": given, "gems": give_gems})


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
    content="width=device-width, initial-scale=1.0, minimum-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover"
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



.ps99-create{padding:14px;min-width:900px;max-width:100%;box-sizing:border-box}.ps99-toolbar{display:flex;gap:10px;align-items:center}.ps99-search,.ps99-sort,.gem-input{height:42px;border:1px solid #2a3b55;background:#0b1625;color:#eef4ff;border-radius:9px;padding:0 13px;outline:none}.ps99-search{flex:1}.ps99-sort{min-width:150px}.ps99-wagerbar{margin-top:10px;padding:13px 16px;border:1px solid #263a55;background:#0e1a2b;border-radius:10px;display:flex;align-items:center;gap:18px}.ps99-wagerbar small,.ps99-create-bottom small{display:block;color:#8494ac;font-size:9px;letter-spacing:.14em;font-weight:900;margin-bottom:5px}.ps99-wagerbar>div:first-child{flex:1}.ps99-balance{display:flex;align-items:center;gap:7px;color:#8fa0b8;font-size:11px}.gem-input{width:155px;min-width:155px;background:#0b1625 !important;color:#eef4ff !important;cursor:text;caret-color:#fff}.gem-input::placeholder{color:#71819a}.gem-input:focus{border-color:#4386ff;box-shadow:0 0 0 2px #4386ff22}.max-btn{border:0;background:transparent;color:#9aabc2;font-weight:900;cursor:pointer}.ps99-inventory-grid{height:390px;margin-top:12px;border:1px dashed #30435f;border-radius:12px;background:#111b2d;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));grid-auto-rows:145px;align-content:start;gap:10px;padding:12px;overflow:auto}.ps99-item{border:1px solid #2b3c57;background:#172238;border-radius:10px;padding:7px;cursor:pointer;aspect-ratio:1/1;min-height:0;height:auto;position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;overflow:hidden}.ps99-item.selected{border-color:#4386ff;box-shadow:0 0 0 2px #4386ff33}.ps99-item img{width:78%;height:58%;object-fit:contain;flex:0 0 auto}.ps99-item-name{font-size:9px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ps99-item-value{font-size:10px;color:#68d7ff;font-weight:900;margin-top:4px}.ps99-qty{position:absolute;right:6px;top:6px;background:#3c82f6;color:white;border-radius:7px;padding:3px 6px;font-size:9px;font-weight:900}.ps99-empty{grid-column:1/-1;display:grid;place-items:center;color:#93a3ba;text-align:center}.ps99-create-bottom{display:flex;align-items:end;gap:10px;margin-top:13px}.ps99-create-bottom>div{min-width:max-content}.ps99-create-bottom .compact{display:flex;align-items:center;gap:5px}.ps99-create-bottom .color-option{width:38px;height:38px;border:2px solid transparent}.ps99-create-bottom .color-option.selected{border-color:white}.ps99-create-bottom .create-submit{margin-left:auto}.give-item-grid,.join-item-grid{height:440px !important;grid-template-columns:repeat(5,minmax(0,1fr)) !important;grid-auto-rows:150px !important;align-content:start !important;gap:10px !important;padding:12px !important;box-sizing:border-box}.give-item-grid .ps99-item,.join-item-grid .ps99-item{width:100%;height:150px;min-height:150px;aspect-ratio:auto;box-sizing:border-box;padding:8px}.give-item-grid .ps99-item img,.join-item-grid .ps99-item img{width:76%;height:70%;max-height:105px;object-fit:contain;flex:0 0 auto}.give-item-grid .ps99-item-name,.join-item-grid .ps99-item-name{width:100%;max-width:100%;font-size:10px;line-height:13px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.give-item-grid .ps99-item-value,.join-item-grid .ps99-item-value{font-size:10px;margin-top:2px}.join-item-grid{height:260px !important;grid-auto-rows:112px !important;grid-template-columns:repeat(5,minmax(0,1fr)) !important}.join-item-grid .ps99-item{height:112px;min-height:112px;padding:6px}.join-item-grid .ps99-item img{height:60%;max-height:62px}.join-item-grid .ps99-item-name{font-size:8px;line-height:10px}.join-item-grid .ps99-item-value{font-size:9px}.top-center-wallet .balance-box{min-width:120px}.top-center-wallet .wallet-btn{white-space:nowrap}@media(max-width:950px){.ps99-create{min-width:0}.ps99-inventory-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.give-item-grid,.join-item-grid{grid-template-columns:repeat(4,minmax(0,1fr)) !important}}@media(max-width:650px){.give-item-grid,.join-item-grid{grid-template-columns:repeat(3,minmax(0,1fr)) !important}.give-item-grid{grid-auto-rows:135px !important}.give-item-grid .ps99-item{height:135px;min-height:135px}.give-item-grid .ps99-item img{height:66%;max-height:88px}.join-item-grid{grid-auto-rows:105px !important;height:230px !important}.join-item-grid .ps99-item{height:105px;min-height:105px}.join-item-grid .ps99-item img{height:58%;max-height:60px}}

/* Mobile chat drawer */
.mobile-chat-button{display:none;position:fixed;right:16px;bottom:16px;z-index:90;border:1px solid #3b5f91;background:linear-gradient(135deg,#347ff5,#6b5cff);color:#fff;border-radius:999px;padding:12px 17px;font-weight:1000;box-shadow:0 10px 30px rgba(0,0,0,.35);cursor:pointer}
.mobile-chat-close{display:none}

/* Create color picker: keep color selection independent from the 1/2 selector. */
.create-color-picker{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:8px}
.create-color-picker .color-option{width:100%;height:44px;padding:0;border:2px solid transparent;border-radius:10px;color:#fff;font-size:11px;font-weight:1000;text-transform:uppercase;cursor:pointer;box-sizing:border-box}
.create-color-picker .color-option.selected{border-color:#fff;box-shadow:0 0 0 3px rgba(255,255,255,.10),0 0 18px rgba(255,255,255,.10)}
.create-color-picker .color-option:active{transform:scale(.98)}
.create-color-count{display:flex;gap:6px}
.create-color-count .count-btn{min-width:52px}

@media(max-width:850px){
    .mobile-chat-button{display:block}
    .mobile-chat-close{display:grid;place-items:center;width:38px;height:38px;border:1px solid #34445d;border-radius:10px;background:#202a3e;color:#fff;font-size:23px;line-height:1;cursor:pointer}
    .chat.mobile-open{display:flex!important;position:fixed;left:0;right:0;top:64px;bottom:0;width:100%;height:auto;z-index:180;border-left:0;background:#0c1523}
    body.mobile-chat-open{overflow:hidden}
    .chat.mobile-open .chat-head{height:58px;flex:0 0 58px;padding:0 14px}
    .chat.mobile-open .chat-head .mobile-chat-title{display:flex;align-items:center;gap:9px}
    .ps99-create{padding:10px!important;min-width:0!important;width:100%;}
    .ps99-toolbar{display:grid;grid-template-columns:1fr 1fr;gap:7px}
    .ps99-toolbar .ps99-search{grid-column:1/-1;width:100%;box-sizing:border-box}
    .ps99-toolbar .ps99-sort{min-width:0;width:100%;box-sizing:border-box}
    .ps99-wagerbar{display:grid;grid-template-columns:1fr auto;gap:9px;padding:10px}
    .ps99-balance{grid-column:1/-1;display:grid;grid-template-columns:auto 1fr;gap:7px}
    .ps99-balance .gem-input{width:100%;min-width:0;box-sizing:border-box}
    .ps99-inventory-grid{grid-template-columns:repeat(3,minmax(0,1fr));height:300px;grid-auto-rows:100px;padding:8px;gap:7px}
    .ps99-create-bottom{display:grid;grid-template-columns:1fr 1fr;align-items:stretch;gap:9px}
    .ps99-create-bottom>div{min-width:0}
    .ps99-create-bottom>div:nth-child(3){grid-column:1/-1}
    .ps99-create-bottom .compact{width:100%}
    .ps99-create-bottom .compact .count-btn{flex:1}
    .create-color-picker{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}
    .create-color-picker .color-option{height:42px}
    .ps99-create-bottom>.secondary,.ps99-create-bottom>.create-submit{width:100%;box-sizing:border-box}
}
.wallet-panel{display:flex;flex-direction:column;gap:12px}.wallet-balance{font-size:28px;font-weight:950;text-align:center}.wallet-actions{display:flex;justify-content:center}.wallet-item-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));grid-auto-rows:minmax(0,1fr);gap:10px;max-height:480px;overflow:auto;padding:8px;border:1px dashed #30435f;border-radius:12px;background:#111b2d}@media(max-width:800px){.wallet-item-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
.start-game-btn{height:44px;padding:0 24px;border:0;border-radius:10px;background:linear-gradient(135deg,#2f7df4,#4d79ff);color:#fff;font-weight:950;cursor:pointer}.match-wagers-row{display:flex;align-items:center;justify-content:center;gap:12px;width:100%;min-height:72px}.match-wager-side{display:flex;align-items:center;justify-content:center;gap:7px;max-width:42%;overflow:visible;flex-wrap:nowrap}.match-wager-item{width:54px;height:54px;flex:0 0 54px;border:1px solid #30425d;border-radius:9px;background:#101b2b;padding:3px;position:relative;box-sizing:border-box}.match-wager-item img{width:100%;height:100%;object-fit:contain;display:block}.match-wager-item span{position:absolute;left:50%;transform:translateX(-50%);bottom:-18px;width:72px;text-align:center;font-size:8px;color:#8fa0b7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.match-wager-gems{font-weight:950;color:#68d7ff;white-space:nowrap}.no-wager-items{color:#55657c;font-size:10px}

/* PS99-style match cards: player avatars left, wager items in the middle, total pot on the right. */
.match.ps99-match-card {
    min-height: 104px;
    grid-template-columns: minmax(280px, 1.35fr) minmax(360px, 1.75fr) minmax(130px, .7fr) 108px;
    gap: 16px;
    padding: 12px 14px;
}
.ps99-match-wagers { display:flex; align-items:center; justify-content:center; gap:14px; min-width:0; }
.ps99-wager-player { display:flex; align-items:center; gap:5px; min-width:0; }
.ps99-wager-player-items { display:flex; gap:4px; max-width:150px; overflow:hidden; }
.ps99-wager-item { width:42px; height:42px; border:1px solid #30425d; border-radius:8px; background:#0d1727; padding:3px; flex:0 0 42px; position:relative; }
.ps99-wager-item img { width:100%; height:100%; object-fit:contain; }
.ps99-wager-item-value { position:absolute; left:2px; right:2px; bottom:2px; padding:2px 3px; border-radius:4px; background:rgba(4,10,18,.82); color:#72dcff; font-size:7px; font-weight:950; text-align:center; }
.ps99-wager-sep { color:#5f718b; font-weight:950; }
.ps99-wager-gem-value{font-size:12px;font-weight:950;color:#eef4ff;white-space:nowrap;margin-bottom:4px;text-align:center}.ps99-match-dice{display:flex;gap:5px;align-items:center;justify-content:center;margin-left:8px}.ps99-match-die{width:28px;height:28px;border-radius:6px;border:1px solid rgba(255,255,255,.18);display:grid;place-items:center;box-shadow:inset 0 0 0 1px rgba(0,0,0,.18)}.ps99-match-die i{width:7px;height:7px;border-radius:50%;background:#fff;box-shadow:0 0 4px rgba(255,255,255,.45)}
.ps99-match-pot { text-align:center; }
.ps99-match-pot-value { font-size:17px; font-weight:1000; color:#eef4ff; }
.ps99-match-pot-label { margin-top:3px; color:#7f91aa; font-size:8px; font-weight:950; letter-spacing:.12em; }
.ps99-match-card > .status { display:none; }
.ps99-match-card > .actions { grid-column:4; }
.ps99-match-card{min-height:128px;}
.ps99-match-card .players{align-self:stretch;display:flex;align-items:center;}
.ps99-match-card .player-mini .avatar,.ps99-match-card .player-mini .waiting-avatar{width:54px;height:54px;}
.ps99-match-card .player-mini-name{max-width:105px;font-size:11px;}
.ps99-match-result { margin-top:3px; color:#dfe9ff; font-size:11px; font-weight:950; }
@media(max-width:1000px){.match.ps99-match-card{grid-template-columns:1.15fr 1.7fr 100px 95px}.ps99-wager-item{width:58px;min-width:58px}.ps99-wager-item-image-wrap{width:48px;height:48px}.ps99-wager-item-name{width:58px}.ps99-match-live-dice-wrap{min-width:108px}}
@media(max-width:720px){.match.ps99-match-card{grid-template-columns:1fr 1fr;}.ps99-match-wagers{grid-column:1/-1;order:2}.ps99-match-pot{order:3}.match.ps99-match-card .actions{order:4}}


.ps99-wager-groups{display:flex;align-items:center;justify-content:center;gap:14px;min-width:0;overflow:hidden;}
.ps99-wager-player{display:flex;flex-direction:column;align-items:center;justify-content:center;min-width:0;}
.ps99-wager-player-items{display:flex;flex-direction:row;align-items:flex-start;gap:8px;margin-top:7px;max-width:100%;overflow:hidden;}
.ps99-wager-item{width:72px;min-width:72px;display:flex;flex-direction:column;align-items:center;gap:3px;padding:0;position:relative;}
.ps99-wager-item-image-wrap{width:58px;height:58px;border:1px solid #344865;border-radius:10px;background:#0d1729;display:grid;place-items:center;overflow:hidden;box-shadow:0 5px 14px rgba(0,0,0,.22);}
.ps99-wager-item img{width:100%;height:100%;object-fit:contain;display:block;}
.ps99-wager-item-name{width:72px;font-size:8px;font-weight:800;color:#c9d5e8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:center;}
.ps99-wager-item-value{font-size:9px;font-weight:950;color:#5edbff;white-space:nowrap;text-align:center;}
.gem-wager-item .ps99-wager-item-image-wrap{font-size:30px;background:#102b4d;border-color:#2563a8;}
.ps99-diamond-image{display:grid;place-items:center;}
.ps99-wager-sep{flex:0 0 auto;font-size:11px;color:#6f819b;font-weight:950;margin-top:12px;}
.ps99-match-wagers{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;min-width:0;}
.ps99-match-live-dice-wrap{display:flex;align-items:center;justify-content:center;min-width:132px;}
.ps99-match-live-dice-wrap .mini-live-title{font-size:9px;font-weight:950;letter-spacing:.08em;color:#8fa3bd;min-width:58px;text-align:center}.ps99-match-live-dice-wrap .mini-rolls{display:flex;gap:6px;}
.ps99-match-live-dice-wrap .mini-die{width:30px;height:30px;border-radius:7px;background:#2a394e;border:1px solid #3d506c;display:block;transition:transform .18s,opacity .18s,background .18s;box-shadow:inset 0 0 0 1px rgba(255,255,255,.03);}
.ps99-match-live-dice-wrap .mini-die.mini-rolling{animation:miniDieShake .16s infinite alternate;}
@keyframes miniDieShake{from{transform:translateY(-1px) rotate(-3deg);opacity:.72}to{transform:translateY(1px) rotate(3deg);opacity:1}}

/* Final PS99-style match list presentation */
.ps99-wager-groups{gap:22px;align-items:center;}
.ps99-wager-player{min-width:90px;}
.ps99-wager-player-items{gap:10px;margin-top:0;overflow:visible;}
.ps99-wager-item{width:82px;min-width:82px;height:82px;display:block;padding:0;border:0;background:transparent;}
.ps99-wager-item-image-wrap{width:82px;height:82px;border-radius:12px;border:1px solid #344865;background:#101b2c;box-shadow:0 6px 18px rgba(0,0,0,.24);}
.ps99-wager-item img{width:100%;height:100%;object-fit:contain;}
.ps99-wager-item-name,.ps99-wager-item-value,.ps99-wager-gem-value{display:none!important;}
.gem-wager-item .ps99-wager-item-image-wrap{font-size:42px;}
.ps99-match-live-dice-wrap{min-width:160px;}
.ps99-match-live-dice-wrap .mini-rolls{gap:8px;}
.ps99-match-live-dice-wrap .mini-die{width:34px;height:34px;}
.ps99-match-card{min-height:148px;}
.game-wager-item-list{display:flex;flex-direction:column;gap:12px;margin-top:12px;}
.game-wager-item-row{display:flex;align-items:center;gap:14px;padding:8px 0;}
.game-wager-thumb{width:62px;height:62px;flex:0 0 62px;border-radius:11px;border:1px solid #30435f;background:#101b2b;display:grid;place-items:center;overflow:hidden;box-shadow:0 5px 15px rgba(0,0,0,.22);}
.game-wager-thumb img{width:100%;height:100%;object-fit:contain;}
.game-wager-gems{font-size:34px;}
.game-wager-item-info{min-width:0;}
.game-wager-item-name{font-size:16px;font-weight:950;color:#eef4ff;line-height:1.15;}
.game-wager-item-value{margin-top:5px;font-size:14px;font-weight:950;color:#61ddff;}
@media(max-width:900px){.ps99-wager-item,.ps99-wager-item-image-wrap{width:64px;min-width:64px;height:64px}.ps99-match-live-dice-wrap{min-width:120px}.ps99-match-live-dice-wrap .mini-die{width:28px;height:28px}.game-wager-thumb{width:52px;height:52px;flex-basis:52px}.game-wager-item-name{font-size:13px}}

/* Mobile overhaul: keep the lobby usable and keep chat visible on phones. */
.join-modal-wrap{width:100%;box-sizing:border-box}.join-help{color:#aab7ca;margin:0 0 12px}.join-color-grid{margin-bottom:12px}.join-wager-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:12px 0}.join-wager-summary>div{padding:10px;border:1px solid #263952;background:#0e1a2b;border-radius:9px;min-width:0}.join-wager-summary span{display:block;font-size:8px;letter-spacing:.1em;color:#8192ab;font-weight:950;margin-bottom:4px}.join-wager-summary b{display:block;font-size:11px;color:#eef4ff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.join-gems-row{display:flex;gap:8px;align-items:center;margin:8px 0 10px}.join-gems-row .gem-input{flex:1;width:auto;min-width:0}.join-gems-row .max-btn{height:42px;padding:0 13px;border:1px solid #30425d;border-radius:9px;background:#17253a;color:#dce7f7}.join-items-label{font-size:9px;letter-spacing:.12em;color:#8798b1;font-weight:950}.join-items-label span{color:#5e718e}.join-submit{width:100%;margin-top:10px}

@media(max-width:1200px){
    .chat{display:flex;width:280px}
    .layout{grid-template-columns:58px minmax(0,1fr)}
    .main{grid-column:2}
}
@media(max-width:720px){
    .topbar{height:58px;padding:0 8px;gap:7px}
    .layout{display:block;min-height:100vh;padding-top:58px}
    .sidebar{display:none}
    .main{padding:9px 8px 245px;min-width:0}
    .chat{display:flex;position:fixed;left:0;right:0;top:auto;bottom:0;width:auto;height:225px;z-index:50;border-left:0;border-top:1px solid #2b3b55;box-shadow:0 -12px 30px rgba(0,0,0,.35)}
    .chat-head{height:40px;min-height:40px;padding:0 11px}
    .chat-list{padding:6px 8px}
    .chat-msg{padding:6px 5px;gap:8px}
    .chat-text{font-size:11px}
    .chat-input{padding:7px}
    .chat-input input{padding:9px}
    .chat-input button{width:40px;padding:8px}
    .top-center-wallet{margin-left:auto;gap:4px;min-width:0}
    .top-center-wallet .balance-box{min-width:0;padding:6px 8px}
    .top-center-wallet .balance-box strong{font-size:11px}
    .top-center-wallet .wallet-btn{padding:7px 8px;font-size:10px}
    .top-actions{gap:4px}
    .top-actions button{padding:7px 8px;font-size:10px}
    .brand-mark{width:34px;height:34px}
    .match.ps99-match-card{grid-template-columns:1fr;display:grid;gap:8px;min-height:0;padding:10px}
    .ps99-match-card .players{grid-column:1;order:1;justify-content:center}
    .ps99-match-wagers{grid-column:1;order:2;grid-template-columns:1fr;gap:8px}
    .ps99-match-live-dice-wrap{grid-column:1;order:3;min-width:0}
    .ps99-match-pot{grid-column:1;order:4}
    .match.ps99-match-card .actions{grid-column:1;order:5;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
    .match.ps99-match-card .actions button{width:100%;min-width:0}
    .ps99-wager-groups{gap:9px}
    .ps99-wager-item{width:60px;min-width:60px;height:60px}
    .ps99-wager-item-image-wrap{width:60px;height:60px}
    .ps99-match-live-dice-wrap .mini-die{width:28px;height:28px}
    .ps99-create{min-width:0!important;width:100%;padding:10px!important}
    .ps99-inventory-grid{grid-template-columns:repeat(3,minmax(0,1fr))!important;grid-auto-rows:96px;height:230px}
    .ps99-item{height:88px!important;min-height:88px!important;padding:5px}
    .ps99-item img{width:58px!important;height:52px!important}
    .ps99-item-name{font-size:8px!important;line-height:10px}
    .ps99-item-value{font-size:8px!important}
    .give-item-grid,.join-item-grid{grid-template-columns:repeat(3,minmax(0,1fr))!important;grid-auto-rows:96px!important;height:230px!important}
    .give-item-grid .ps99-item,.join-item-grid .ps99-item{height:88px!important;min-height:88px!important}
    .give-item-grid .ps99-item img,.join-item-grid .ps99-item img{height:52px!important;max-height:52px!important}
    .modal{width:calc(100vw - 16px)!important;max-width:calc(100vw - 16px)!important;max-height:calc(100vh - 16px)!important;border-radius:12px}
    .modal-head{padding:10px 12px}
    #generalContent{padding:12px!important;overflow-x:hidden}
    .join-wager-summary{grid-template-columns:1fr}
    .join-gems-row{flex-direction:column;align-items:stretch}
    .join-gems-row .gem-input{width:100%;box-sizing:border-box}
    .join-gems-row .max-btn{width:100%}
    .color-grid{grid-template-columns:repeat(3,minmax(0,1fr))!important}
    .color-option{min-width:0!important;width:100%!important}
    .game-top{grid-template-columns:1fr!important;gap:8px}
    .roll-center{order:0}
    .game-player-card{padding:10px}
}


/* ============================================================
   FINAL MOBILE POLISH
   - Chat is hidden until the Chat button is pressed.
   - No mobile bottom chat strip.
   - Cleaner PS99-style cards and spacing.
   - Prevent browser pinch/double-tap zoom.
   ============================================================ */
@media (max-width: 720px) {
    html, body {
        width: 100%;
        min-width: 0;
        max-width: 100%;
        overflow-x: hidden;
        overscroll-behavior-x: none;
        -webkit-text-size-adjust: 100%;
        text-size-adjust: 100%;
        touch-action: pan-y;
    }

    body {
        padding: 0;
        background:
            radial-gradient(circle at 50% -12%, rgba(55,110,220,.20), transparent 38%),
            #080d17;
    }

    /* Never reserve space for the hidden chat panel. */
    .main {
        padding: 9px 8px 92px !important;
        min-height: calc(100vh - 58px);
    }

    /* The desktop chat must be completely gone on mobile. */
    .chat#chatPanel {
        display: none !important;
        visibility: hidden;
        pointer-events: none;
    }

    /* Only this state can make chat visible. */
    .chat#chatPanel.mobile-open {
        display: flex !important;
        visibility: visible;
        pointer-events: auto;
        position: fixed !important;
        inset: 0 !important;
        width: 100% !important;
        height: 100dvh !important;
        min-height: 100dvh !important;
        z-index: 9999 !important;
        border: 0 !important;
        border-radius: 0 !important;
        background: #080f1b !important;
        box-shadow: none !important;
    }

    .chat#chatPanel.mobile-open .chat-head {
        height: 62px;
        min-height: 62px;
        padding: max(8px, env(safe-area-inset-top)) 14px 8px;
        background: rgba(12,21,35,.96);
        border-bottom: 1px solid #263650;
    }

    .chat#chatPanel.mobile-open .chat-list {
        padding: 10px 10px calc(10px + env(safe-area-inset-bottom));
    }

    .chat#chatPanel.mobile-open .chat-input {
        padding: 9px 10px max(9px, env(safe-area-inset-bottom));
        background: #0c1523;
    }

    .mobile-chat-button {
        display: flex !important;
        align-items: center;
        justify-content: center;
        gap: 7px;
        right: 14px;
        bottom: max(14px, env(safe-area-inset-bottom));
        min-height: 44px;
        padding: 0 17px;
        border: 1px solid rgba(106,157,255,.55);
        border-radius: 999px;
        background: linear-gradient(135deg, #347ff5 0%, #6657ff 100%);
        box-shadow: 0 12px 32px rgba(20,66,150,.38), 0 0 0 1px rgba(255,255,255,.05) inset;
        font-size: 12px;
        font-weight: 1000;
        -webkit-tap-highlight-color: transparent;
    }

    body.mobile-chat-open .mobile-chat-button {
        display: none !important;
    }

    .mobile-chat-title {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 15px;
        font-weight: 1000;
    }

    .mobile-chat-close {
        width: 38px;
        height: 38px;
        border-radius: 11px;
        border: 1px solid #30415d;
        background: #172337;
        color: #fff;
        font-size: 23px;
    }

    /* Compact, cleaner mobile top bar. */
    .topbar {
        height: 58px !important;
        padding: 0 8px !important;
        gap: 6px !important;
        position: sticky;
        top: 0;
        z-index: 80;
        backdrop-filter: blur(18px);
        -webkit-backdrop-filter: blur(18px);
        background: rgba(8,13,23,.88) !important;
        border-bottom: 1px solid rgba(50,67,94,.65);
    }

    .brand-mark {
        width: 34px !important;
        height: 34px !important;
        border-radius: 10px !important;
    }

    .top-center-wallet {
        margin-left: auto !important;
        gap: 5px !important;
        min-width: 0 !important;
    }

    .top-center-wallet .balance-box {
        min-width: 0 !important;
        max-width: 145px;
        padding: 6px 9px !important;
        border-radius: 10px !important;
    }

    .top-center-wallet .balance-box strong {
        font-size: 11px !important;
    }

    .top-center-wallet .wallet-btn {
        padding: 7px 9px !important;
        border-radius: 9px !important;
        font-size: 10px !important;
    }

    .top-actions {
        gap: 4px !important;
    }

    .top-actions button {
        padding: 7px 8px !important;
        border-radius: 9px !important;
        font-size: 10px !important;
    }

    /* Mobile hero / lobby cards. */
    .lobby-hero {
        grid-template-columns: 1fr !important;
        gap: 14px !important;
        min-height: 0 !important;
        padding: 20px 16px !important;
        margin-bottom: 12px !important;
        border-radius: 17px !important;
        background:
            radial-gradient(circle at 88% 10%, rgba(108,76,255,.30), transparent 34%),
            linear-gradient(145deg, #16253e, #0f1727 72%) !important;
    }

    .lobby-hero h1 {
        font-size: 27px !important;
    }

    .lobby-hero p {
        font-size: 11px !important;
        line-height: 1.55 !important;
    }

    .lobby-actions {
        display: grid !important;
        grid-template-columns: 1fr 1fr !important;
        gap: 8px !important;
    }

    /* Stats become a clean horizontal strip. */
    .stats {
        display: grid !important;
        grid-template-columns: repeat(3, minmax(0,1fr)) !important;
        gap: 7px !important;
        overflow: visible !important;
        white-space: normal !important;
        margin-bottom: 12px !important;
    }

    .stats > * {
        min-width: 0 !important;
        padding: 9px 5px !important;
        border-radius: 11px !important;
    }

    .stats b,
    .stats strong {
        font-size: 13px !important;
    }

    /* Match cards: stacked PS99-style layout, no cramped columns. */
    .match.ps99-match-card {
        grid-template-columns: 1fr !important;
        display: grid !important;
        gap: 9px !important;
        min-height: 0 !important;
        padding: 11px !important;
        margin-bottom: 9px !important;
        border-radius: 15px !important;
        border: 1px solid #263752 !important;
        background: linear-gradient(145deg, #111c2c, #0d1624) !important;
        box-shadow: 0 8px 22px rgba(0,0,0,.16) !important;
    }

    .ps99-match-card .players,
    .ps99-match-card .color-row,
    .ps99-match-card .wager,
    .ps99-match-card .status,
    .ps99-match-card .actions {
        grid-column: 1 !important;
    }

    .ps99-match-card .players {
        order: 1 !important;
    }

    .ps99-match-card .color-row {
        order: 2 !important;
    }

    .ps99-match-card .match-live {
        order: 3 !important;
        min-height: 74px !important;
        border-radius: 13px !important;
    }

    .ps99-match-card .wager {
        order: 4 !important;
        text-align: center !important;
    }

    .ps99-match-card .status {
        order: 5 !important;
        text-align: center !important;
    }

    .ps99-match-card .actions {
        order: 6 !important;
        display: grid !important;
        grid-template-columns: repeat(2, minmax(0,1fr)) !important;
        gap: 7px !important;
    }

    .ps99-match-card .actions button {
        min-height: 40px !important;
        border-radius: 10px !important;
    }

    .ps99-match-card .item-stack img {
        width: 62px !important;
        height: 62px !important;
    }

    /* Cleaner item/inventory cards. */
    .ps99-inventory-grid {
        grid-template-columns: repeat(3, minmax(0,1fr)) !important;
        gap: 7px !important;
    }

    .ps99-item {
        border-radius: 11px !important;
        padding: 7px !important;
    }

    .ps99-item img {
        width: 72% !important;
        height: 55% !important;
    }

    .ps99-item-name {
        font-size: 9px !important;
    }

    .ps99-item-value {
        font-size: 8px !important;
    }

    /* Create / Join dialogs use almost the whole phone, with no horizontal squeeze. */
    .overlay .modal {
        width: calc(100vw - 16px) !important;
        max-width: none !important;
        max-height: calc(100dvh - 16px) !important;
        border-radius: 17px !important;
        margin: 8px !important;
    }

    .modal-head {
        padding: 13px 14px !important;
    }

    .create-form.ps99-create {
        padding: 10px !important;
    }

    .ps99-toolbar {
        grid-template-columns: 1fr !important;
    }

    .create-color-picker {
        grid-template-columns: repeat(2, minmax(0,1fr)) !important;
        gap: 8px !important;
    }

    .create-color-picker .color-option {
        height: 46px !important;
    }

    /* Prevent accidental double-tap zoom on interactive controls. */
    button, a, input, select, textarea, .ps99-item, .color-option {
        touch-action: manipulation;
    }

    input, select, textarea {
        font-size: 16px !important;
    }
}


/* ============================================================
   COINFLIP
   ============================================================ */
.create-game-tabs{display:flex;gap:8px;margin:0 0 14px}.create-game-tab{flex:1;border:1px solid var(--line);background:#0c1422;color:#9aa9bd;border-radius:10px;padding:12px 14px;font-weight:900;cursor:pointer}.create-game-tab.active{background:#17243a;color:#fff;border-color:#3b82f6}.coinflip-create-choice{padding:16px;border:1px solid #243149;background:#0b1320;border-radius:14px;margin-bottom:14px}.coinflip-create-title{text-align:center;font-size:11px;letter-spacing:2px;color:#8d9bb2;font-weight:900;margin-bottom:12px}.coin-side-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.coin-side-btn{height:100px;border:1px solid #334155;background:#111c2d;color:#fff;border-radius:14px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;cursor:pointer}.coin-side-btn span{font-size:32px;font-weight:1000}.coin-side-btn b{font-size:12px;letter-spacing:2px}.coin-side-btn.active{border-color:#f5c542;box-shadow:0 0 0 2px rgba(245,197,66,.15),0 10px 30px rgba(0,0,0,.22)}.coinflip-game{background:linear-gradient(135deg,#151c2c,#0e1728)}.coin-art{filter:drop-shadow(0 8px 18px rgba(245,197,66,.25))}.coinflip-players{display:grid;grid-template-columns:1fr 1.5fr 1fr;align-items:center;gap:20px;padding:28px 20px 10px}.coin-player{display:flex;flex-direction:column;align-items:center;gap:8px}.coin-player img{width:76px;height:76px;border-radius:50%;object-fit:cover;border:2px solid #2d3b52}.coin-player b{font-size:16px}.coin-player span{font-size:11px;letter-spacing:2px;font-weight:1000;color:#94a3b8}.coinflip-center{text-align:center}.coin-stage{height:190px;display:flex;align-items:center;justify-content:center;perspective:1000px}.coin-object{position:relative;width:118px;height:118px;transform-style:preserve-3d;transition:transform .5s cubic-bezier(.2,.8,.2,1)}.coin-face{position:absolute;inset:0;border-radius:50%;backface-visibility:hidden;display:flex;align-items:center;justify-content:center;font-size:48px;font-weight:1000;background:radial-gradient(circle at 35% 30%,#ffe9a2,#e2aa21 48%,#9a6810);border:6px solid #f8d568;box-shadow:inset 0 0 0 3px rgba(255,255,255,.2),0 18px 45px rgba(0,0,0,.35)}.coin-back{transform:rotateY(180deg)}.show-tails{transform:rotateY(180deg)}.coin-flipping{animation:coinFlip 2.4s cubic-bezier(.2,.7,.2,1) forwards}.coin-result{min-height:52px;margin-top:6px;display:flex;flex-direction:column;gap:4px}.coin-result strong{font-size:25px}.coin-result span{font-size:11px;letter-spacing:2px;color:#9aa9bd}.coin-winner{border-color:#f5c542!important;box-shadow:0 0 0 2px rgba(245,197,66,.15),0 0 35px rgba(245,197,66,.14)}.join-coin-sides{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:15px 0}.join-coin-side{padding:16px;border:1px solid #334155;background:#111c2d;color:#fff;border-radius:12px;font-weight:1000;cursor:pointer}.join-coin-side span{font-size:24px;margin-right:8px}.join-coin-side.selected{border-color:#f5c542;background:#1d2a3e}.join-coin-side.disabled{opacity:.3;cursor:not-allowed}.coin-wagers{margin-top:16px}@keyframes coinFlip{0%{transform:rotateY(0) translateY(0) scale(1)}20%{transform:rotateY(720deg) translateY(-38px) scale(1.05)}45%{transform:rotateY(1620deg) translateY(-80px) scale(1.1)}70%{transform:rotateY(2520deg) translateY(-38px) scale(1.05)}100%{transform:rotateY(3240deg) translateY(0) scale(1)}}
@media(max-width:760px){.coinflip-players{grid-template-columns:1fr 1.4fr 1fr;gap:6px;padding:20px 6px}.coin-player img{width:54px;height:54px}.coin-player b{font-size:12px;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.coin-stage{height:150px}.coin-object{width:88px;height:88px}.coin-face{font-size:36px}.coin-result strong{font-size:19px}}

.mini-coin-wrap{display:flex;align-items:center;gap:7px}.mini-coin{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:1000;background:radial-gradient(circle at 35% 30%,#ffe9a2,#e2aa21 48%,#9a6810);border:2px solid #f8d568;box-shadow:0 5px 12px rgba(0,0,0,.28)}.mini-coin-side-label{font-size:9px;color:#91a0b4;font-weight:900;letter-spacing:1px}.mini-coin-flip{animation:miniCoinFlip .75s linear infinite}.mini-coin-result{box-shadow:0 0 0 2px rgba(245,197,66,.18),0 0 16px rgba(245,197,66,.16)}.coin-mini-side{display:inline-flex;width:22px;height:22px;border-radius:50%;align-items:center;justify-content:center;background:#d9a82d;color:#1a2230;font-size:10px;font-weight:1000;border:1px solid #f5d16b}.coin-mini-side + .coin-mini-side{margin-left:4px}@keyframes miniCoinFlip{0%{transform:rotateY(0) translateY(0)}50%{transform:rotateY(900deg) translateY(-7px)}100%{transform:rotateY(1800deg) translateY(0)}}
<style>
.coin-stage-3d{position:relative;height:285px;min-height:285px;overflow:hidden;background:radial-gradient(circle at 50% 44%,rgba(245,197,66,.10),transparent 34%),radial-gradient(circle at 50% 100%,rgba(59,130,246,.08),transparent 55%);border-radius:22px;}
.coin3d-mount{position:absolute;inset:0;z-index:2;}
.coin3d-canvas{display:block;width:100%;height:100%;filter:drop-shadow(0 18px 35px rgba(0,0,0,.38));}
.coin-floor-glow{position:absolute;left:50%;bottom:28px;width:230px;height:46px;transform:translateX(-50%);border-radius:50%;background:radial-gradient(ellipse,rgba(245,197,66,.24),rgba(245,197,66,0) 70%);filter:blur(8px);z-index:1;pointer-events:none;}
.coin-countdown-number{font-size:58px!important;font-weight:1000!important;text-shadow:0 0 28px rgba(245,197,66,.45);}
.coin-result strong{font-size:28px;text-shadow:0 0 25px rgba(245,197,66,.24);}
@media(max-width:760px){.coin-stage-3d{height:235px;min-height:235px}.coin-floor-glow{bottom:18px;width:180px}.coin-countdown-number{font-size:45px!important}}
.action-busy{opacity:.55!important;cursor:wait!important;pointer-events:none!important}

/* FINAL MATCH-LOBBY WAGER LAYOUT */
.ps99-match-wagers{display:flex;align-items:center;justify-content:center;gap:14px;min-height:92px;padding:8px 12px;overflow:visible!important}
.ps99-wager-groups{display:flex;align-items:center;justify-content:center;gap:18px;flex:1;min-width:0;overflow:visible}
.ps99-wager-player{display:flex;align-items:center;justify-content:center;min-width:0;overflow:visible}
.ps99-wager-player-items{display:flex;align-items:center;justify-content:center;gap:7px;max-width:100%;flex-wrap:nowrap;overflow:visible}
.ps99-wager-item{width:58px;height:58px;flex:0 0 58px;position:relative;overflow:visible;border-radius:10px}
.ps99-wager-item-image-wrap{width:100%;height:100%;border-radius:10px;display:flex;align-items:center;justify-content:center;background:#101b2b;border:1px solid #30425d;box-sizing:border-box;padding:4px;overflow:hidden}
.ps99-wager-item-image-wrap img{width:100%;height:100%;object-fit:contain;display:block}
.ps99-diamond-image{font-size:28px}
.ps99-match-live-dice-wrap{flex:0 0 auto}
@media(max-width:800px){.ps99-match-wagers{gap:8px;padding:7px}.ps99-wager-groups{gap:8px}.ps99-wager-player-items{gap:4px}.ps99-wager-item{width:48px;height:48px;flex-basis:48px}.ps99-diamond-image{font-size:23px}}

.ps99-more-items .ps99-wager-item-image-wrap{font-size:14px;font-weight:950;color:#9badc6;background:#0d1726}
.ps99-wager-item[data-tooltip-title]{cursor:help}
.ps99-wager-groups{flex-wrap:nowrap}
@media(max-width:1050px){.ps99-wager-groups{gap:10px}.ps99-wager-item{width:52px;height:52px;flex-basis:52px}}
</style>
</style>


<style>
/* Match-list item cleanup + compact hover details */
.ps99-match-card{overflow:visible!important;position:relative;isolation:isolate;}
.ps99-match-card .players,.ps99-match-wagers,.ps99-wager-groups,.ps99-wager-player{min-width:0;overflow:visible!important;}
.ps99-wager-groups{gap:10px!important;align-items:center!important;justify-content:center!important;}
.ps99-wager-player{width:auto;max-width:180px;flex:0 1 180px;}
.ps99-wager-player-items{display:flex!important;flex-wrap:wrap!important;justify-content:center!important;align-items:center!important;gap:6px!important;width:100%;max-width:176px!important;overflow:visible!important;}
.ps99-wager-item{width:44px!important;min-width:44px!important;height:44px!important;padding:0!important;border:0!important;background:transparent!important;position:relative!important;overflow:visible!important;cursor:help;}
.ps99-wager-item-image-wrap{width:42px!important;height:42px!important;border-radius:8px!important;overflow:hidden!important;}
.ps99-wager-item img{width:100%!important;height:100%!important;object-fit:contain!important;display:block!important;}
.ps99-wager-item::after{content:attr(data-tooltip-title);position:absolute;left:50%;bottom:calc(100% + 7px);transform:translate(-50%,4px);width:max-content;max-width:180px;padding:5px 7px;border:1px solid #344865;border-radius:7px;background:rgba(8,14,25,.98);box-shadow:0 8px 22px rgba(0,0,0,.38);color:#edf4ff;font-size:9px;font-weight:850;line-height:1.15;white-space:normal;text-align:center;pointer-events:none;opacity:0;visibility:hidden;z-index:9999;transition:opacity .12s ease,transform .12s ease,visibility .12s ease;}
.ps99-wager-item::before{content:attr(data-tooltip-value);position:absolute;left:50%;bottom:calc(100% + 28px);transform:translate(-50%,4px);width:max-content;padding:4px 6px;border-radius:6px;background:#111d30;border:1px solid #2c415f;color:#63ddff;font-size:8px;font-weight:950;white-space:nowrap;pointer-events:none;opacity:0;visibility:hidden;z-index:10000;transition:opacity .12s ease,transform .12s ease,visibility .12s ease;}
.ps99-wager-item:hover::after,.ps99-wager-item:hover::before{opacity:1;visibility:visible;transform:translate(-50%,0);} 
.ps99-wager-item.gem-wager-item::after{bottom:calc(100% + 7px);}
.ps99-wager-item.gem-wager-item::before{bottom:calc(100% + 28px);}
.ps99-wager-player:nth-child(2n) .ps99-wager-item::after,.ps99-wager-player:nth-child(2n) .ps99-wager-item::before{ }
.ps99-match-live-dice-wrap{min-width:120px!important;flex:0 0 auto;}
@media(max-width:1000px){.ps99-wager-player{max-width:150px;flex-basis:150px}.ps99-wager-player-items{max-width:146px!important}.ps99-wager-item{width:40px!important;min-width:40px!important;height:40px!important}.ps99-wager-item-image-wrap{width:38px!important;height:38px!important}.ps99-match-live-dice-wrap{min-width:100px!important;}}
@media(max-width:720px){.ps99-wager-groups{gap:7px!important;flex-wrap:wrap!important;align-content:center}.ps99-wager-player{max-width:120px;flex-basis:120px}.ps99-wager-player-items{max-width:116px!important}.ps99-wager-item{width:38px!important;min-width:38px!important;height:38px!important}.ps99-wager-item-image-wrap{width:36px!important;height:36px!important}.ps99-wager-item::after{max-width:140px;font-size:8px}.ps99-match-live-dice-wrap{min-width:90px!important;}}
/* ABSOLUTE FINAL MATCH ITEM LAYOUT
   Keep wager tiles large and tightly grouped. Gems is rendered as one tile. */
.ps99-match-card .ps99-wager-groups{display:flex!important;flex-wrap:nowrap!important;align-items:center!important;justify-content:center!important;gap:8px!important;min-width:0!important;}
.ps99-match-card .ps99-wager-player{display:flex!important;flex:0 0 auto!important;width:auto!important;max-width:none!important;align-items:center!important;justify-content:center!important;}
.ps99-match-card .ps99-wager-player-items{display:flex!important;flex-wrap:nowrap!important;align-items:center!important;justify-content:flex-start!important;gap:6px!important;width:auto!important;max-width:none!important;margin:0!important;padding:0!important;}
.ps99-match-card .ps99-wager-item{width:86px!important;height:86px!important;min-width:86px!important;max-width:86px!important;flex:0 0 86px!important;margin:0!important;padding:0!important;}
.ps99-match-card .ps99-wager-item-image-wrap{width:86px!important;height:86px!important;min-width:86px!important;max-width:86px!important;border-radius:13px!important;padding:5px!important;margin:0!important;}
.ps99-match-card .ps99-wager-item img{width:100%!important;height:100%!important;object-fit:contain!important;}
.ps99-match-card .ps99-wager-item::after{font-size:9px!important;max-width:180px!important;}
.ps99-match-card .ps99-wager-item::before{font-size:8px!important;}
@media(max-width:1050px){.ps99-match-card .ps99-wager-item,.ps99-match-card .ps99-wager-item-image-wrap{width:72px!important;height:72px!important;min-width:72px!important;max-width:72px!important;}.ps99-match-card .ps99-wager-player-items{gap:5px!important;}}
@media(max-width:720px){.ps99-match-card .ps99-wager-groups{gap:5px!important;overflow-x:auto!important;max-width:100%!important;scrollbar-width:none!important;}.ps99-match-card .ps99-wager-groups::-webkit-scrollbar{display:none!important;}.ps99-match-card .ps99-wager-item,.ps99-match-card .ps99-wager-item-image-wrap{width:62px!important;height:62px!important;min-width:62px!important;max-width:62px!important;}.ps99-match-card .ps99-wager-player-items{gap:4px!important;}}


/* ============================================================
   FINAL PS99 MATCH WAGER LAYOUT OVERRIDE
   One compact row per player. Large square item tiles. No drifting.
   ============================================================ */
.ps99-match-card .ps99-match-wagers{
    display:grid!important;
    grid-template-columns:minmax(0,1fr) auto!important;
    align-items:center!important;
    justify-items:center!important;
    gap:14px!important;
    min-width:0!important;
    width:100%!important;
    overflow:visible!important;
}
.ps99-match-card .ps99-wager-groups{
    display:flex!important;
    align-items:center!important;
    justify-content:center!important;
    gap:12px!important;
    width:100%!important;
    min-width:0!important;
    overflow:visible!important;
}
.ps99-match-card .ps99-wager-player{
    display:flex!important;
    align-items:center!important;
    justify-content:center!important;
    min-width:0!important;
    flex:0 1 auto!important;
}
.ps99-match-card .ps99-wager-player-items{
    display:flex!important;
    flex-direction:row!important;
    align-items:center!important;
    justify-content:center!important;
    gap:8px!important;
    flex-wrap:nowrap!important;
    width:max-content!important;
    max-width:none!important;
    margin:0!important;
    overflow:visible!important;
}
.ps99-match-card .ps99-wager-item{
    width:82px!important;
    height:82px!important;
    min-width:82px!important;
    max-width:82px!important;
    flex:0 0 82px!important;
    display:block!important;
    position:relative!important;
    overflow:visible!important;
    padding:0!important;
    border:0!important;
    background:transparent!important;
}
.ps99-match-card .ps99-wager-item-image-wrap{
    width:82px!important;
    height:82px!important;
    min-width:82px!important;
    border-radius:13px!important;
    border:1px solid #344865!important;
    background:#101b2c!important;
    display:grid!important;
    place-items:center!important;
    box-sizing:border-box!important;
    padding:5px!important;
    overflow:hidden!important;
}
.ps99-match-card .ps99-wager-item img{
    width:100%!important;
    height:100%!important;
    object-fit:contain!important;
    display:block!important;
}
.ps99-match-card .ps99-wager-item-name,
.ps99-match-card .ps99-wager-item-value,
.ps99-match-card .ps99-wager-gem-value{
    display:none!important;
}
.ps99-match-card .gem-wager-item .ps99-wager-item-image-wrap{
    font-size:42px!important;
    background:#102b4d!important;
    border-color:#2563a8!important;
}
.ps99-match-card .ps99-more-items .ps99-wager-item-image-wrap{
    font-size:24px!important;
    font-weight:950!important;
    color:#d9e8ff!important;
}
.ps99-match-card .ps99-match-live-dice-wrap{
    min-width:118px!important;
    display:flex!important;
    align-items:center!important;
    justify-content:center!important;
    flex:0 0 auto!important;
}

@media (max-width:1100px){
    .ps99-match-card .ps99-wager-item,
    .ps99-match-card .ps99-wager-item-image-wrap{
        width:72px!important;
        height:72px!important;
        min-width:72px!important;
    }
    .ps99-match-card .gem-wager-item .ps99-wager-item-image-wrap{font-size:36px!important}
}
@media (max-width:720px){
    .ps99-match-card .ps99-match-wagers{
        grid-template-columns:1fr!important;
        gap:10px!important;
    }
    .ps99-match-card .ps99-wager-groups{
        gap:8px!important;
        width:100%!important;
        overflow-x:auto!important;
        justify-content:flex-start!important;
        padding:3px 4px!important;
        scrollbar-width:none!important;
    }
    .ps99-match-card .ps99-wager-groups::-webkit-scrollbar{display:none}
    .ps99-match-card .ps99-wager-item,
    .ps99-match-card .ps99-wager-item-image-wrap{
        width:68px!important;
        height:68px!important;
        min-width:68px!important;
    }
    .ps99-match-card .gem-wager-item .ps99-wager-item-image-wrap{font-size:34px!important}
}



/* ============================================================
   PREMIUM MOBILE UI — layout/structure preserved
   ============================================================ */
@media (max-width: 720px) {
    :root { --m-pad: 10px; }

    * { box-sizing: border-box; }
    html, body { width:100%; max-width:100%; overflow-x:hidden; }
    body { font-size:14px; -webkit-tap-highlight-color:transparent; }

    /* ---------- top navigation ---------- */
    .topbar {
        height: 60px !important;
        padding: 7px 10px !important;
        gap: 7px !important;
        display:flex !important;
        align-items:center !important;
        position:sticky !important;
        top:0 !important;
        z-index:200 !important;
        background:rgba(7,12,21,.91) !important;
        border-bottom:1px solid rgba(76,96,126,.30) !important;
        box-shadow:0 8px 28px rgba(0,0,0,.18) !important;
    }
    .brand { flex:0 0 auto !important; min-width:34px !important; }
    .brand-mark {
        width:38px !important; height:38px !important;
        border-radius:12px !important;
        display:grid !important; place-items:center !important;
        font-size:18px !important;
    }
    .brand-text { display:none !important; }
    .top-center-wallet {
        flex:1 1 auto !important;
        margin:0 !important;
        min-width:0 !important;
        display:flex !important;
        justify-content:center !important;
    }
    .top-center-wallet .balance-box {
        height:38px !important;
        max-width:none !important;
        width:auto !important;
        min-width:0 !important;
        padding:0 11px !important;
        border-radius:12px !important;
        gap:6px !important;
    }
    .top-center-wallet .balance-box strong {
        max-width:92px !important;
        overflow:hidden !important;
        text-overflow:ellipsis !important;
        white-space:nowrap !important;
        font-size:12px !important;
    }
    .top-center-wallet .wallet-btn {
        height:38px !important;
        padding:0 11px !important;
        border-radius:12px !important;
        font-size:10px !important;
        white-space:nowrap !important;
    }
    .top-actions { flex:0 0 auto !important; gap:5px !important; }
    .top-actions .user-chip {
        width:38px !important; height:38px !important;
        padding:3px !important; border-radius:12px !important;
        justify-content:center !important;
    }
    .top-actions .user-chip img { width:30px !important; height:30px !important; border-radius:9px !important; }
    .top-actions .user-chip span { display:none !important; }
    .top-actions > button.secondary { display:none !important; }
    .top-actions .login-btn { height:38px !important; padding:0 11px !important; border-radius:12px !important; font-size:10px !important; }

    /* ---------- page shell ---------- */
    .layout { display:block !important; min-height:100dvh !important; padding-top:60px !important; }
    .main { padding:10px 10px 92px !important; min-width:0 !important; width:100% !important; }

    /* ---------- home / hero ---------- */
    .lobby-hero {
        min-height:0 !important;
        padding:20px 17px !important;
        margin:0 0 10px !important;
        border-radius:20px !important;
        display:flex !important;
        align-items:center !important;
        gap:10px !important;
        overflow:hidden !important;
    }
    .lobby-hero > div:first-child { min-width:0 !important; flex:1 1 auto !important; }
    .lobby-hero h1 { font-size:28px !important; line-height:1.03 !important; letter-spacing:-.8px !important; margin:7px 0 9px !important; }
    .lobby-hero p { font-size:11px !important; line-height:1.55 !important; max-width:245px !important; }
    .lobby-kicker { font-size:8px !important; letter-spacing:.14em !important; }
    .lobby-hero-art { width:104px !important; min-width:104px !important; height:120px !important; position:relative !important; }
    .lobby-die { transform:scale(.78) !important; }
    .lobby-cta { display:grid !important; grid-template-columns:1fr 1fr !important; gap:7px !important; margin-top:13px !important; }
    .lobby-cta button { min-height:40px !important; padding:0 8px !important; border-radius:11px !important; font-size:10px !important; }

    .lobby-cats {
        display:flex !important;
        gap:6px !important;
        overflow-x:auto !important;
        padding:2px 1px 5px !important;
        margin-bottom:12px !important;
        scrollbar-width:none !important;
        -webkit-overflow-scrolling:touch !important;
    }
    .lobby-cats::-webkit-scrollbar { display:none !important; }
    .lobby-cat {
        flex:0 0 auto !important;
        min-height:34px !important;
        padding:0 12px !important;
        border-radius:999px !important;
        font-size:9px !important;
        white-space:nowrap !important;
    }

    .lobby-section { margin-bottom:14px !important; }
    .lobby-section-head { margin-bottom:8px !important; }
    .lobby-section-head h2 { font-size:15px !important; }
    .lobby-section-head button,.live-label { font-size:8px !important; }
    .lobby-games { grid-template-columns:1fr 1fr !important; gap:8px !important; }
    .lobby-game { min-height:154px !important; padding:12px !important; border-radius:16px !important; }
    .lobby-game .game-art { width:50px !important; height:50px !important; font-size:27px !important; }
    .lobby-game h3 { font-size:14px !important; margin-top:9px !important; }
    .lobby-game p { font-size:9px !important; line-height:1.35 !important; }
    .game-play-pill { font-size:8px !important; padding:6px 8px !important; }

    /* ---------- game heading / toolbar ---------- */
    .game-app-heading {
        position:sticky !important; top:60px !important; z-index:80 !important;
        margin:0 -10px 8px !important; padding:8px 10px !important;
        background:rgba(8,13,23,.91) !important;
        backdrop-filter:blur(16px) !important;
        border-bottom:1px solid rgba(61,80,109,.28) !important;
    }
    .game-app-heading span { font-size:13px !important; font-weight:950 !important; }
    .back-lobby-btn { min-height:34px !important; padding:0 10px !important; border-radius:10px !important; font-size:9px !important; }
    .toolbar {
        display:grid !important;
        grid-template-columns:minmax(0,1fr) auto auto !important;
        gap:6px !important;
        margin-bottom:8px !important;
    }
    .toolbar > .primary,.toolbar > .secondary,.toolbar select { min-height:38px !important; border-radius:11px !important; font-size:9px !important; }
    .toolbar-right { display:contents !important; }
    .toolbar select { max-width:105px !important; min-width:0 !important; padding:0 7px !important; }
    .toolbar-right > button.secondary { width:38px !important; padding:0 !important; }

    .stats { gap:6px !important; margin-bottom:9px !important; }
    .stats > * { min-height:48px !important; padding:7px 4px !important; border-radius:12px !important; font-size:8px !important; display:flex !important; flex-direction:column !important; justify-content:center !important; align-items:center !important; gap:2px !important; }
    .stats b,.stats strong { font-size:12px !important; }

    /* ---------- match cards ---------- */
    .match.ps99-match-card {
        display:flex !important;
        flex-direction:column !important;
        gap:8px !important;
        padding:10px !important;
        margin-bottom:8px !important;
        border-radius:17px !important;
        overflow:hidden !important;
    }
    .ps99-match-card .players { width:100% !important; min-width:0 !important; }
    .ps99-match-card .player-mini { min-width:0 !important; }
    .ps99-match-card .player-mini img { width:34px !important; height:34px !important; border-radius:10px !important; }
    .ps99-match-card .player-mini b { max-width:92px !important; font-size:10px !important; overflow:hidden !important; text-overflow:ellipsis !important; white-space:nowrap !important; }
    .ps99-match-card .player-mini small { font-size:8px !important; }

    .ps99-match-card .ps99-match-wagers {
        display:flex !important;
        flex-direction:row !important;
        align-items:center !important;
        justify-content:center !important;
        gap:7px !important;
        width:100% !important;
        min-width:0 !important;
        padding:7px 2px !important;
        overflow:hidden !important;
    }
    .ps99-match-card .ps99-wager-groups {
        flex:0 1 auto !important;
        width:auto !important;
        max-width:100% !important;
        min-width:0 !important;
        display:flex !important;
        align-items:center !important;
        justify-content:center !important;
        gap:5px !important;
        flex-wrap:nowrap !important;
        overflow-x:auto !important;
        scrollbar-width:none !important;
        -webkit-overflow-scrolling:touch !important;
    }
    .ps99-match-card .ps99-wager-groups::-webkit-scrollbar { display:none !important; }
    .ps99-match-card .ps99-wager-player {
        flex:0 0 auto !important;
        width:auto !important;
        max-width:none !important;
        min-width:0 !important;
    }
    .ps99-match-card .ps99-wager-player-items {
        display:flex !important;
        flex-wrap:nowrap !important;
        align-items:center !important;
        gap:5px !important;
        width:max-content !important;
        max-width:none !important;
    }
    .ps99-match-card .ps99-wager-item,
    .ps99-match-card .ps99-wager-item-image-wrap {
        width:66px !important;
        min-width:66px !important;
        max-width:66px !important;
        height:66px !important;
        min-height:66px !important;
        max-height:66px !important;
        border-radius:13px !important;
    }
    .ps99-match-card .ps99-wager-item-image-wrap { padding:4px !important; }
    .ps99-match-card .ps99-wager-item::after { font-size:8px !important; max-width:130px !important; }
    .ps99-match-card .ps99-wager-item::before { font-size:7px !important; }
    .ps99-match-card .gem-wager-item .ps99-wager-item-image-wrap { font-size:31px !important; }

    .ps99-match-card .ps99-match-live-dice-wrap {
        width:100% !important;
        min-width:0 !important;
        min-height:58px !important;
        border-radius:13px !important;
        padding:7px !important;
    }
    .ps99-match-card .ps99-match-live-dice-wrap .mini-die { width:30px !important; height:30px !important; }
    .ps99-match-card .ps99-match-pot { width:100% !important; padding:7px !important; border-radius:11px !important; }
    .ps99-match-card .ps99-match-pot-value { font-size:11px !important; }
    .ps99-match-card .ps99-match-pot-label,.ps99-match-card .ps99-match-result { font-size:7px !important; }
    .ps99-match-card .status { width:100% !important; min-height:20px !important; }
    .ps99-match-card .status .score { font-size:10px !important; }
    .ps99-match-card .actions { width:100% !important; display:grid !important; grid-template-columns:repeat(2,minmax(0,1fr)) !important; gap:6px !important; }
    .ps99-match-card .actions button { min-height:38px !important; border-radius:11px !important; font-size:9px !important; }

    /* ---------- match game screen ---------- */
    .game-top { gap:7px !important; }
    .game-player-card { padding:9px !important; border-radius:14px !important; min-width:0 !important; }
    .game-player-card img { width:38px !important; height:38px !important; }
    .game-player-card b { font-size:10px !important; max-width:100px !important; overflow:hidden !important; text-overflow:ellipsis !important; white-space:nowrap !important; }
    .roll-center { min-width:0 !important; }
    .roll-center .dice { gap:5px !important; }
    .roll-center .die { width:45px !important; height:45px !important; }
    .game-status { font-size:10px !important; }
    .game-countdown { font-size:42px !important; }
    .game-actions { grid-template-columns:1fr 1fr !important; gap:6px !important; }
    .game-actions button { min-height:40px !important; border-radius:11px !important; }

    /* ---------- wallet / inventory ---------- */
    .ps99-inventory-grid,.give-item-grid,.join-item-grid {
        grid-template-columns:repeat(3,minmax(0,1fr)) !important;
        gap:6px !important;
        grid-auto-rows:92px !important;
        max-height:300px !important;
        height:auto !important;
    }
    .ps99-item,.give-item-grid .ps99-item,.join-item-grid .ps99-item {
        height:86px !important; min-height:86px !important; padding:5px !important; border-radius:11px !important;
    }
    .ps99-item img,.give-item-grid .ps99-item img,.join-item-grid .ps99-item img { width:54px !important; height:48px !important; object-fit:contain !important; }
    .ps99-item-name { font-size:7px !important; line-height:9px !important; }
    .ps99-item-value { font-size:7px !important; }

    /* ---------- modals ---------- */
    .modal-overlay { padding:8px !important; align-items:flex-end !important; }
    .modal {
        width:100% !important; max-width:100% !important;
        max-height:calc(100dvh - 10px) !important;
        margin:0 !important; border-radius:18px 18px 12px 12px !important;
        overflow:hidden !important;
    }
    .modal-head { min-height:48px !important; padding:9px 12px !important; }
    .modal-head h3 { font-size:13px !important; }
    #generalContent { padding:11px !important; max-height:calc(100dvh - 70px) !important; overflow-y:auto !important; overflow-x:hidden !important; -webkit-overflow-scrolling:touch !important; }
    .color-grid { gap:6px !important; }
    .color-option { min-height:62px !important; border-radius:12px !important; }
    .join-wager-summary { grid-template-columns:1fr 1fr !important; gap:6px !important; }
    .join-wager-summary > div { padding:8px !important; }

    /* ---------- chat ---------- */
    .mobile-chat-button { right:12px !important; bottom:max(12px,env(safe-area-inset-bottom)) !important; min-height:43px !important; padding:0 15px !important; border-radius:999px !important; }
}

@media (max-width:390px) {
    .top-center-wallet .wallet-btn { display:none !important; }
    .top-center-wallet .balance-box { max-width:145px !important; }
    .lobby-games { grid-template-columns:1fr !important; }
    .lobby-game { min-height:135px !important; }
    .toolbar { grid-template-columns:minmax(0,1fr) 38px 38px !important; }
    .toolbar > .primary { font-size:8px !important; }
    .toolbar select { display:none !important; }
    .ps99-match-card .ps99-wager-item,.ps99-match-card .ps99-wager-item-image-wrap { width:60px !important; min-width:60px !important; height:60px !important; min-height:60px !important; }
}


/* ============================================================
   MOBILE PREMIUM MATCH LOBBY — clean modern betting-site layout
   Desktop is untouched. Applies only to phones/tablets.
   ============================================================ */
@media (max-width: 720px) {
    .match.ps99-match-card {
        position:relative!important;
        display:grid!important;
        grid-template-columns:minmax(0,1fr)!important;
        gap:0!important;
        padding:0!important;
        margin:0 0 10px!important;
        border:1px solid rgba(75,96,128,.30)!important;
        border-radius:18px!important;
        background:linear-gradient(180deg,rgba(18,28,44,.98),rgba(10,17,29,.98))!important;
        box-shadow:0 10px 28px rgba(0,0,0,.22)!important;
        overflow:hidden!important;
    }

    .ps99-match-card .players {
        display:flex!important;
        align-items:center!important;
        justify-content:center!important;
        gap:7px!important;
        width:100%!important;
        padding:10px 11px 8px!important;
        border-bottom:1px solid rgba(74,95,125,.20)!important;
        background:rgba(255,255,255,.012)!important;
    }
    .ps99-match-card .player-mini {
        display:flex!important;
        flex:1 1 0!important;
        align-items:center!important;
        justify-content:center!important;
        gap:6px!important;
        min-width:0!important;
        padding:5px 7px!important;
        border-radius:11px!important;
        background:rgba(255,255,255,.025)!important;
    }
    .ps99-match-card .player-mini-line { min-width:0!important; gap:6px!important; }
    .ps99-match-card .player-mini img,
    .ps99-match-card .player-mini .avatar,
    .ps99-match-card .player-mini .waiting-avatar {
        width:31px!important;height:31px!important;min-width:31px!important;
        border-radius:9px!important;
    }
    .ps99-match-card .player-mini-name {
        max-width:78px!important;font-size:9px!important;font-weight:900!important;
        overflow:hidden!important;text-overflow:ellipsis!important;white-space:nowrap!important;
    }
    .ps99-match-card .player-mini small { font-size:7px!important; }
    .ps99-match-card .vs {
        flex:0 0 auto!important;
        width:22px!important;height:22px!important;
        display:grid!important;place-items:center!important;
        border:1px solid rgba(91,113,146,.34)!important;
        border-radius:7px!important;background:#101b2c!important;
        color:#8393ab!important;font-size:7px!important;font-weight:950!important;
    }

    /* Wager area: one tight centered strip, never two drifting item groups. */
    .ps99-match-card .ps99-match-wagers {
        display:flex!important;
        flex-direction:column!important;
        align-items:stretch!important;
        justify-content:center!important;
        gap:7px!important;
        width:100%!important;
        padding:9px 10px 8px!important;
        background:rgba(6,12,21,.34)!important;
        overflow:hidden!important;
    }
    .ps99-match-card .ps99-wager-groups {
        display:flex!important;
        align-items:center!important;
        justify-content:flex-start!important;
        width:100%!important;
        max-width:100%!important;
        min-height:72px!important;
        padding:2px 2px!important;
        gap:5px!important;
        overflow-x:auto!important;
        overflow-y:visible!important;
        flex-wrap:nowrap!important;
        scrollbar-width:none!important;
        -webkit-overflow-scrolling:touch!important;
        scroll-snap-type:x proximity!important;
    }
    .ps99-match-card .ps99-wager-groups::-webkit-scrollbar { display:none!important; }
    .ps99-match-card .ps99-wager-player {
        flex:0 0 auto!important;width:auto!important;max-width:none!important;
        min-width:0!important;display:flex!important;align-items:center!important;
    }
    .ps99-match-card .ps99-wager-player-items {
        display:flex!important;align-items:center!important;justify-content:flex-start!important;
        flex-wrap:nowrap!important;width:max-content!important;max-width:none!important;
        gap:5px!important;margin:0!important;padding:0!important;
    }
    .ps99-match-card .ps99-wager-item,
    .ps99-match-card .ps99-wager-item-image-wrap {
        width:58px!important;height:58px!important;min-width:58px!important;max-width:58px!important;
        min-height:58px!important;max-height:58px!important;
        border-radius:12px!important;flex:0 0 58px!important;
    }
    .ps99-match-card .ps99-wager-item-image-wrap {
        padding:4px!important;
        background:linear-gradient(145deg,#18263b,#0d1625)!important;
        border:1px solid rgba(84,108,142,.28)!important;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.04),0 4px 12px rgba(0,0,0,.18)!important;
    }
    .ps99-match-card .ps99-wager-item img { object-fit:contain!important; }
    .ps99-match-card .gem-wager-item .ps99-wager-item-image-wrap {
        display:grid!important;place-items:center!important;font-size:28px!important;
    }
    .ps99-match-card .ps99-more-items .ps99-wager-item-image-wrap {
        font-size:14px!important;font-weight:950!important;color:#aebbd0!important;
    }
    .ps99-match-card .ps99-match-live-dice-wrap {
        order:2!important;width:100%!important;min-width:0!important;
        min-height:46px!important;height:46px!important;padding:5px 8px!important;
        border:1px solid rgba(73,96,130,.24)!important;border-radius:11px!important;
        background:rgba(17,27,43,.70)!important;
        display:flex!important;align-items:center!important;justify-content:center!important;
    }
    .ps99-match-card .ps99-match-live-dice-wrap .mini-die { width:26px!important;height:26px!important; }

    /* Pot + status become a compact information row. */
    .ps99-match-card .ps99-match-pot {
        display:grid!important;
        grid-template-columns:minmax(0,1fr) auto!important;
        grid-template-rows:auto auto!important;
        align-items:center!important;
        width:100%!important;
        min-height:47px!important;
        padding:7px 11px!important;
        border:0!important;border-top:1px solid rgba(74,95,125,.18)!important;
        border-radius:0!important;background:rgba(255,255,255,.018)!important;
    }
    .ps99-match-card .ps99-match-pot-value {
        grid-row:1 / span 2!important;text-align:left!important;
        font-size:13px!important;font-weight:950!important;color:#f2f7ff!important;
    }
    .ps99-match-card .ps99-match-pot-label,
    .ps99-match-card .ps99-match-result {
        text-align:right!important;font-size:7px!important;font-weight:900!important;
        letter-spacing:.08em!important;color:#73849d!important;
    }
    .ps99-match-card .ps99-match-result { color:#92a3bb!important; }

    .ps99-match-card .status {
        width:100%!important;min-height:29px!important;padding:0 10px 7px!important;
        display:flex!important;align-items:center!important;justify-content:center!important;
        background:rgba(255,255,255,.012)!important;
    }
    .ps99-match-card .status .score {
        min-height:24px!important;padding:5px 12px!important;
        border-radius:999px!important;background:#101c2e!important;
        border:1px solid rgba(80,105,141,.25)!important;
        font-size:8px!important;font-weight:950!important;letter-spacing:.06em!important;
        color:#9eb0c8!important;
    }

    /* Action bar: Join/Call Bot + Cancel on top, View gets the full-width row. */
    .ps99-match-card .actions {
        display:grid!important;
        grid-template-columns:minmax(0,1fr) minmax(0,1fr)!important;
        gap:6px!important;
        width:100%!important;
        padding:0 10px 10px!important;
        background:rgba(255,255,255,.012)!important;
    }
    .ps99-match-card .actions button {
        width:100%!important;min-width:0!important;min-height:40px!important;
        padding:0 10px!important;border-radius:11px!important;
        font-size:9px!important;font-weight:950!important;letter-spacing:.01em!important;
        box-shadow:none!important;
        -webkit-tap-highlight-color:transparent!important;
    }
    .ps99-match-card .actions .view {
        grid-column:1 / -1!important;
        min-height:42px!important;
        border:1px solid rgba(91,127,174,.42)!important;
        background:linear-gradient(180deg,#1b314d,#14243a)!important;
        color:#eaf4ff!important;
        box-shadow:0 5px 14px rgba(0,0,0,.18)!important;
        order:10!important;
    }
    .ps99-match-card .actions .join,
    .ps99-match-card .actions .callbot {
        border:1px solid rgba(44,183,125,.30)!important;
        background:rgba(20,96,72,.34)!important;
    }
    .ps99-match-card .actions .cancel {
        border:1px solid rgba(205,83,103,.25)!important;
        background:rgba(102,35,51,.28)!important;
    }

    /* Touch feedback without the desktop hover jump. */
    .ps99-match-card .actions button:active { transform:scale(.985)!important; }
}

@media (max-width: 390px) {
    .ps99-match-card .players { padding-left:8px!important;padding-right:8px!important; }
    .ps99-match-card .player-mini { padding-left:5px!important;padding-right:5px!important; }
    .ps99-match-card .player-mini-name { max-width:65px!important;font-size:8px!important; }
    .ps99-match-card .ps99-wager-item,
    .ps99-match-card .ps99-wager-item-image-wrap { width:54px!important;height:54px!important;min-width:54px!important;max-width:54px!important; }
    .ps99-match-card .ps99-wager-item { flex-basis:54px!important; }
    .ps99-match-card .ps99-wager-player-items { gap:4px!important; }
}
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
            <strong id="topBalance">{{ format_gems(wallet_total_value(user["id"], user["balance"])) }}</strong>
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
        onclick="enterGame()"
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
            <article class="lobby-game purple" onclick="enterGame('colordice')" role="button" tabindex="0">
                <div class="game-art">🎲</div><h3>ColorDice</h3><p>Choose your color · Roll to win</p>
                <span class="game-play-pill">PLAY NOW ↗</span>
            </article>
            <article class="lobby-game coinflip-game" onclick="enterGame('coinflip')" role="button" tabindex="0">
                <div class="game-art coin-art">🪙</div><h3>Coinflip</h3><p>Pick Heads or Tails · Flip to win</p>
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
    <div class="game-app-heading"><button class="back-lobby-btn" onclick="showLobby()">← Lobby</button><span id="gameAppTitle">ColorDice Game</span></div>
    <div class="toolbar">

        <button
            class="primary"
            onclick="openCreate(currentOriginalGame)"
        >
            ＋ Create <span id="gameCreateLabel">ColorDice</span>
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


<aside class="chat" id="chatPanel">

    <div class="chat-head">

        <div class="mobile-chat-title">
            <button class="mobile-chat-close" type="button" onclick="closeMobileChat()" aria-label="Close chat">×</button>
            <span>Live Chat</span>
        </div>

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

<button id="mobileChatButton" class="mobile-chat-button" type="button" onclick="openMobileChat()">💬 Chat</button>

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
            <span id="createModalTitle">🎲 Create Color Dice Duel</span>
        </div>

        <button
            class="close"
            onclick="closeModal('createOverlay')"
        >
            ×
        </button>

    </div>


    <div class="create-form ps99-create">
        <div class="create-game-tabs">
            <button id="createColorDiceTab" class="create-game-tab active" onclick="setCreateGame('colordice')">🎲 ColorDice</button>
            <button id="createCoinflipTab" class="create-game-tab" onclick="setCreateGame('coinflip')">🪙 Coinflip</button>
        </div>
        <div id="coinflipChoice" class="coinflip-create-choice" style="display:none">
            <div class="coinflip-create-title">CHOOSE YOUR SIDE</div>
            <div class="coin-side-grid">
                <button id="createHeadsBtn" class="coin-side-btn heads active" onclick="selectCoinSide('heads')"><span>H</span><b>HEADS</b></button>
                <button id="createTailsBtn" class="coin-side-btn tails" onclick="selectCoinSide('tails')"><span>T</span><b>TAILS</b></button>
            </div>
        </div>
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
            <div id="colorDiceModeControls">
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
                <div class="create-color-count">
                    <button id="oneColorBtn" class="count-btn active" onclick="setColorCount(1)">1 COLOR</button>
                    <button id="twoColorBtn" class="count-btn" onclick="setColorCount(2)">2 COLORS</button>
                </div>
                <div class="create-color-picker">
                    {% for name, hex in colors.items() %}<button type="button" class="color-option" data-color="{{ name }}" style="background:{{ hex }}" onclick="selectColor('{{ name }}',this,event);event.stopPropagation();">{{ name }}</button>{% endfor %}
                </div>
            </div>
            </div>
            <button class="secondary" type="button" onclick="showGeneral('Rules','Choose your game and wager. Coinflip uses Heads or Tails. All items are virtual website-side items.')">☷ Rules</button>
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


<script src="https://cdn.jsdelivr.net/npm/three@0.180.0/build/three.min.js"></script>
<script>

const COLORS =
    {{ colors|tojson }};

const PAGE_USER_ID = {{ user["id"] if user else "null" }};
const MAX_BET_CLIENT = 5000000000000;

let selectedColors = [];

let selectedColorCount = 1;
let selectedMatchMode = "1v1";
let selectedCreateGame = "colordice";
let currentOriginalGame = "colordice";
let gameLobbyToken = 0;
let selectedCoinSide = "heads";
let pendingJoinMatchId = null;
let pendingJoinColors = [];
let pendingJoinItems = [];
let pendingJoinGems = 0;
let joinInventory = [];
let previousMatchStatuses = {};
let previousMatchParticipants = {};
const autoOpenedMatches = new Set();

let currentMatchId = null;
const activeGameAnimations = new Set();

// Live match state.
// These MUST be declared so the live-update timer can run.
let liveMatches = [];
let matchAnimationTimer = null;
let lastMatchListSignature = "";

function normalizeGameType(value) {
    const v = String(value || "colordice").trim().toLowerCase();
    return (v === "coinflip" || v === "coin-flip" || v === "flip") ? "coinflip" : "colordice";
}
function matchGameType(match) {
    const raw = String(match?.game_type || "").trim().toLowerCase();
    if (["coinflip", "coin-flip", "flip"].includes(raw)) return "coinflip";
    // A 1v1v1 match is only supported by ColorDice in this site.
    // Treat it as ColorDice even if an old DB row has a missing/legacy
    // game_type value.
    if (String(match?.match_mode || "").toLowerCase() === "1v1v1") return "colordice";
    // Legacy/mislabelled rows can still be identified by the stored side fields.
    if (["heads", "tails"].includes(String(match?.creator_side || "").toLowerCase())) return "coinflip";
    if (["heads", "tails"].includes(String(match?.opponent_side || "").toLowerCase())) return "coinflip";
    if (["heads", "tails"].includes(String(match?.coin_result || "").toLowerCase())) return "coinflip";
    return "colordice";
}


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

function wagerTotal(playerBet, items) {
    const itemValue = (items || []).reduce((sum, x) => sum + Number(x.value || 0) * Number(x.quantity || 1), 0);
    return Number(playerBet || 0) + itemValue;
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

function setCreateGame(type) {
    selectedCreateGame = type === "coinflip" ? "coinflip" : "colordice";
    const flip = selectedCreateGame === "coinflip";
    document.getElementById("createColorDiceTab")?.classList.toggle("active", !flip);
    document.getElementById("createCoinflipTab")?.classList.toggle("active", flip);
    const choice = document.getElementById("coinflipChoice");
    if (choice) choice.style.display = flip ? "block" : "none";
    const colorControls = document.getElementById("colorDiceModeControls");
    if (colorControls) colorControls.style.display = flip ? "none" : "contents";
    const title = document.getElementById("createModalTitle");
    if (title) title.textContent = flip ? "🪙 Create Coinflip" : "🎲 Create Color Dice Duel";
    updateWagerSummary();
}

function selectCoinSide(side) {
    selectedCoinSide = side === "tails" ? "tails" : "heads";
    document.getElementById("createHeadsBtn")?.classList.toggle("active", selectedCoinSide === "heads");
    document.getElementById("createTailsBtn")?.classList.toggle("active", selectedCoinSide === "tails");
}

async function openCreate(gameType = selectedCreateGame || "colordice") {

    setCreateGame(gameType);
    selectedCoinSide = "heads";
    selectCoinSide("heads");

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
        .querySelectorAll("#createOverlay .color-option")
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


function selectColor(color,button,event) {

    if (event) { event.preventDefault(); event.stopPropagation(); }

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

    const creatingGameType = selectedCreateGame === "coinflip" ? "coinflip" : "colordice";
    const creatingCoinSide = selectedCoinSide === "tails" ? "tails" : "heads";
    const creatingColors = [...selectedColors];
    const creatingColorCount = selectedColorCount;
    const creatingMode = selectedMatchMode;
    const creatingItems = [...selectedInventoryIds];

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

        if (bet > MAX_BET_CLIENT) {

            toast(
                "Maximum Gems wager is 5T (5000B)."
            );

            return;
        }

        if (
            creatingGameType === "colordice" &&
            creatingColors.length !== creatingColorCount
        ) {

            toast(
                `Choose exactly ${creatingColorCount} color${creatingColorCount > 1 ? "s" : ""}.`
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
                    game_type: creatingGameType,
                    side: creatingCoinSide,
                    colors: creatingGameType === "colordice" ? creatingColors : [],
                    mode: creatingGameType === "colordice" ? creatingMode : "1v1",
                    best_of: creatingGameType === "colordice" && creatingMode.startsWith("first") ? Number(creatingMode.slice(5)) : 1,
                    items: creatingItems
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

        closeModal("createOverlay");

        // Keep the game type selected in the create modal and the visible
        // Originals lobby in sync. This also handles creating Coinflip from
        // the main lobby's quick-create button.
        const createdGameType = creatingGameType;
        currentOriginalGame = createdGameType;
        selectedCreateGame = createdGameType;
        if (document.getElementById("gameApp")?.style.display !== "block") {
            enterGame(createdGameType);
        } else {
            enterGame(createdGameType);
        }

        toast(createdGameType === "coinflip" ? "Coinflip match created." : "ColorDice match created.");

        // Insert the server-confirmed match immediately so a successful create
        // cannot look like it vanished while the next lobby poll is in flight.
        if (data.match && Number(data.match.id) === Number(data.match_id)) {
            liveMatches = [data.match, ...liveMatches.filter(m => Number(m.id) !== Number(data.match_id))];
            lastMatchListSignature = "";
        }

        await refreshMe();
        await loadMatches(true);
        // If the server list poll races or a legacy row has a mismatched type,
        // keep the just-created server-confirmed row visible in this game's lobby.
        if (data.match && matchGameType(data.match) === normalizeGameType(currentOriginalGame)) {
            const stillPresent = liveMatches.some(m => Number(m.id) === Number(data.match_id));
            if (!stillPresent) liveMatches.unshift(data.match);
            lastMatchListSignature = "";
            loadMatches(true);
        }

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


function lockActionButton(button, ms=900) {
    if (!button) return false;
    if (button.dataset.busy === "1") return true;
    button.dataset.busy = "1";
    button.disabled = true;
    button.classList.add("action-busy");
    setTimeout(() => {
        button.dataset.busy = "0";
        button.disabled = false;
        button.classList.remove("action-busy");
    }, ms);
    return false;
}

async function joinMatch(id) {
    const response = await fetch(`/api/matches/${id}`);
    const payload = await response.json();
    if (!response.ok || !payload.ok) { toast("Could not load match."); return; }

    const match = payload.match;
    if (match.game_type === "coinflip") {
        pendingJoinMatchId = id;
        pendingJoinItems = [];
        pendingJoinGems = 0;
        try { const ir = await fetch("/api/inventory", {credentials:"same-origin"}); const idata = await ir.json(); joinInventory = idata.items || []; } catch(e) { joinInventory = []; }
        window.__joinMatch = match;
        try {
            const mr = await fetch("/api/me", {credentials:"same-origin", cache:"no-store"});
            const md = await mr.json();
            window.__joinBalance = Number(md.balance || 0);
        } catch(e) { window.__joinBalance = 0; }
        const creatorTotal = Number(match.creator_total_value || 0);
        const minTotal = creatorTotal * 0.90, maxTotal = creatorTotal * 1.10;
        showGeneral("Join Coinflip", `
            <div class="join-modal-wrap">
                <p class="join-help">Choose the side not taken by the creator.</p>
                <div class="join-coin-sides">
                    <button id="joinHeadsBtn" class="join-coin-side ${match.creator_side === "heads" ? "disabled" : ""}" ${match.creator_side === "heads" ? "disabled" : ""} onclick="selectJoinCoinSide('heads')"><span>H</span> HEADS</button>
                    <button id="joinTailsBtn" class="join-coin-side ${match.creator_side === "tails" ? "disabled" : ""}" ${match.creator_side === "tails" ? "disabled" : ""} onclick="selectJoinCoinSide('tails')"><span>T</span> TAILS</button>
                </div>
                <div class="join-wager-summary"><div><span>CREATOR TOTAL</span><b>💎 ${formatGemsClient(creatorTotal)}</b></div><div><span>ALLOWED RANGE</span><b>💎 ${formatGemsClient(minTotal)} – ${formatGemsClient(maxTotal)}</b></div><div><span>YOUR TOTAL</span><b id="joinTotalPreview">💎 0</b></div></div>
                <div class="join-gems-row"><input id="joinGemsInput" class="gem-input" placeholder="Gems e.g. 1B" oninput="updateJoinWagerPreview()"><button class="max-btn" type="button" onclick="autoSelectJoinWager()">AUTO SELECT</button></div>
                <div class="join-items-label">YOUR VIRTUAL ITEMS</div><div id="joinInventoryGrid" class="ps99-inventory-grid join-item-grid" style="margin-top:7px">${joinInventory.length ? joinInventory.map(x => `<div class="ps99-item" data-item-id="${escapeHtml(x.item_id)}" onclick="toggleJoinItem('${escapeHtml(x.item_id)}',this)"><img src="${escapeHtml(x.image_url || '')}"><div class="ps99-item-name">${escapeHtml(x.item_name)}</div><div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div></div>`).join("") : '<div class="ps99-empty">No virtual items.</div>'}</div>
                <button class="create-submit join-submit" onclick="submitJoinMatch()">JOIN FLIP</button>
            </div>`);
        window.__pendingJoinCoinSide = match.creator_side === "heads" ? "tails" : "heads";
        updateJoinWagerPreview();
        return;
    }
    const count = (match.creator_colors || []).length || 1;
    const taken = [...(match.creator_colors || []), ...(match.opponent_colors || [])];
    pendingJoinMatchId = id;
    pendingJoinColors = [];
    pendingJoinItems = [];
    pendingJoinGems = 0;

    try {
        const ir = await fetch("/api/inventory", {credentials:"same-origin"});
        const idata = await ir.json();
        joinInventory = idata.items || [];
    } catch(e) {
        joinInventory = [];
    }

    window.__joinMatch = match;
    try {
        const mr = await fetch("/api/me", {credentials:"same-origin", cache:"no-store"});
        const md = await mr.json();
        window.__joinBalance = Number(md.balance || 0);
    } catch(e) { window.__joinBalance = 0; }
    const creatorTotal = Number(match.creator_total_value || 0);
    const minTotal = creatorTotal * 0.90;
    const maxTotal = creatorTotal * 1.10;

    showGeneral("Join Match", `
        <div class="join-modal-wrap">
            <p class="join-help">Choose exactly ${count} color${count > 1 ? "s" : ""}. Creator colors are unavailable.</p>

            <div class="color-grid join-color-grid">
                ${Object.entries(COLORS).map(([name,hex]) => `
                    <button class="color-option ${taken.includes(name) ? "disabled" : ""}"
                        data-join-color="${name}"
                        style="background:${hex};${taken.includes(name) ? "opacity:.28;cursor:not-allowed;filter:grayscale(.7)" : ""}"
                        ${taken.includes(name) ? "disabled" : ""}
                        onclick="selectJoinColor('${name}',this)">${name}</button>
                `).join("")}
            </div>

            <div class="join-wager-summary">
                <div><span>CREATOR TOTAL</span><b>💎 ${formatGemsClient(creatorTotal)}</b></div>
                <div><span>ALLOWED RANGE</span><b>💎 ${formatGemsClient(minTotal)} – ${formatGemsClient(maxTotal)}</b></div>
                <div><span>YOUR TOTAL</span><b id="joinTotalPreview">💎 0</b></div>
            </div>

            <div class="join-gems-row">
                <input id="joinGemsInput" class="gem-input" placeholder="Gems e.g. 1B" oninput="updateJoinWagerPreview()">
                <button class="max-btn" type="button" onclick="autoSelectJoinWager()">AUTO SELECT</button>
            </div>

            <div class="join-items-label">YOUR VIRTUAL ITEMS <span>· AUTO SELECT = 1 ITEM</span></div>
            <div id="joinInventoryGrid" class="ps99-inventory-grid join-item-grid" style="margin-top:7px">
                ${joinInventory.length ? joinInventory.map(x => `
                    <div class="ps99-item" data-item-id="${escapeHtml(x.item_id)}" onclick="toggleJoinItem('${escapeHtml(x.item_id)}',this)">
                        <img src="${escapeHtml(x.image_url || '')}">
                        <div class="ps99-item-name">${escapeHtml(x.item_name)}</div>
                        <div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div>
                    </div>
                `).join("") : '<div class="ps99-empty">No virtual items.</div>'}
            </div>

            <button class="create-submit join-submit" onclick="submitJoinMatch()">JOIN MATCH</button>
        </div>
    `);
    updateJoinWagerPreview();
}

function getJoinSelectedItemValue() {
    let total = 0;
    for (const id of pendingJoinItems) {
        const item = (joinInventory || []).find(x => String(x.item_id) === String(id));
        if (item) total += Number(item.value || 0);
    }
    return total;
}

function updateJoinWagerPreview() {
    const input = document.getElementById("joinGemsInput");
    if (input) pendingJoinGems = parseGemInput(input.value || 0);
    const total = getJoinSelectedItemValue() + Number(pendingJoinGems || 0);
    const el = document.getElementById("joinTotalPreview");
    if (el) el.textContent = `💎 ${formatGemsClient(total)}`;
}

function autoSelectJoinWager() {
    const match = window.__joinMatch || {};
    const target = Number(match.creator_total_value || 0);
    const minTotal = target * 0.90;
    const maxTotal = target * 1.10;
    if (!target) { toast("Could not calculate the creator wager."); return; }

    pendingJoinItems = [];
    pendingJoinGems = 0;
    document.querySelectorAll("#joinInventoryGrid .ps99-item.selected").forEach(el => el.classList.remove("selected"));

    const usable = (joinInventory || [])
        .filter(x => Number(x.value || 0) > 0 && Number(x.value || 0) <= maxTotal)
        .slice()
        .sort((a,b) => Number(b.value || 0) - Number(a.value || 0));

    if (!usable.length) {
        pendingJoinGems = target;
        const input = document.getElementById("joinGemsInput");
        if (input) input.value = String(Math.round(target * 100) / 100);
        updateJoinWagerPreview();
        toast("No suitable item found. Gems were selected automatically.");
        return;
    }

    // Select MULTIPLE items greedily while staying under the target.
    // Then add the smallest useful item if needed to enter the 90-110% band.
    let total = 0;
    const selected = [];

    for (const item of usable) {
        const value = Number(item.value || 0);
        if (selected.length >= 20) break;
        if (total + value <= target) {
            selected.push(item);
            total += value;
        }
        if (total >= minTotal) break;
    }

    if (total < minTotal) {
        const candidates = usable
            .filter(x => !selected.some(y => String(y.item_id) === String(x.item_id)))
            .filter(x => total + Number(x.value || 0) <= maxTotal)
            .sort((a,b) => {
                const da = Math.abs((total + Number(a.value || 0)) - target);
                const db = Math.abs((total + Number(b.value || 0)) - target);
                return da - db;
            });
        if (candidates.length) {
            selected.push(candidates[0]);
            total += Number(candidates[0].value || 0);
        }
    }

    // If items alone do not reach 90%, fill the remainder with Gems.
    pendingJoinItems = selected.map(x => String(x.item_id));
    for (const item of selected) {
        const card = document.querySelector(`#joinInventoryGrid .ps99-item[data-item-id="${CSS.escape(String(item.item_id))}"]`);
        if (card) card.classList.add("selected");
    }

    // If the joiner has no Gems, AUTO SELECT must stay item-only.
    // Never invent/select a Gems amount the user cannot actually pay.
    const joinBalance = Number(window.__joinBalance || 0);
    let gems = 0;
    if (joinBalance > 0) {
        gems = Math.max(0, minTotal - total);
        if (total + gems <= maxTotal) {
            gems = Math.max(gems, target - total);
        }
        gems = Math.max(0, Math.min(gems, maxTotal - total, joinBalance));
    }

    pendingJoinGems = gems;
    const input = document.getElementById("joinGemsInput");
    if (input) input.value = String(Math.round(gems * 100) / 100);
    updateJoinWagerPreview();
    toast(`Auto Selected ${selected.length} item${selected.length === 1 ? "" : "s"} · total ${formatGemsClient(total + gems)} Gems`);
}

function toggleJoinItem(id, el) {
    const i = pendingJoinItems.indexOf(id);
    if (i >= 0) {
        pendingJoinItems.splice(i, 1);
        el.classList.remove("selected");
    } else {
        pendingJoinItems.push(id);
        el.classList.add("selected");
    }
    updateJoinWagerPreview();
}

function selectJoinColor(color, button) {
    const match = window.__joinMatch || {};
    const count = (match.creator_colors || []).length || 1;
    const taken = [...(match.creator_colors || []), ...(match.opponent_colors || [])];
    if (taken.includes(color)) return;
    if (pendingJoinColors.includes(color)) {
        pendingJoinColors = pendingJoinColors.filter(c => c !== color);
        button.classList.remove("selected");
    } else if (pendingJoinColors.length < count) {
        pendingJoinColors.push(color);
        button.classList.add("selected");
    } else {
        toast(`Choose exactly ${count} color${count > 1 ? "s" : ""}.`);
    }
}

function selectJoinCoinSide(side) {
    const m = window.__joinMatch || {};
    if (m.creator_side === side) return;
    window.__pendingJoinCoinSide = side;
    document.getElementById("joinHeadsBtn")?.classList.toggle("selected", side === "heads");
    document.getElementById("joinTailsBtn")?.classList.toggle("selected", side === "tails");
}

async function submitJoinMatch() {
    const id = pendingJoinMatchId;
    if (!id) return;
    const mres = await fetch(`/api/matches/${id}`);
    const md = await mres.json();
    if (md.match?.game_type === "coinflip") {
        const total = getJoinSelectedItemValue() + Number(pendingJoinGems || 0);
        const target = Number(md.match?.creator_total_value || 0);
        if (total < target * 0.90 - 0.01 || total > target * 1.10 + 0.01) { toast(`Your total must be ${formatGemsClient(target * 0.90)} – ${formatGemsClient(target * 1.10)} Gems.`); return; }
        const response = await fetch(`/api/matches/${id}/join`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({side:window.__pendingJoinCoinSide || (md.match.creator_side === "heads" ? "tails" : "heads"),items:pendingJoinItems,bet:Number(pendingJoinGems||0)})});
        const data = await response.json();
        if (!response.ok || !data.ok) { toast(data.error || "Could not join."); return; }
        closeModal("generalOverlay"); pendingJoinMatchId=null; await refreshMe(); await loadMatches(); const joinedMatch = liveMatches.find(x => Number(x.id) === Number(id)); if (matchIsFull(joinedMatch) || data.started) await openGame(id); return;
    }
    const count = (md.match?.creator_colors || []).length || 1;
    if (pendingJoinColors.length !== count) { toast(`Choose exactly ${count} color${count > 1 ? "s" : ""}.`); return; }

    const total = getJoinSelectedItemValue() + Number(pendingJoinGems || 0);
    const target = Number(md.match?.creator_total_value || 0);
    if (total < target * 0.90 - 0.01 || total > target * 1.10 + 0.01) {
        toast(`Your total must be ${formatGemsClient(target * 0.90)} – ${formatGemsClient(target * 1.10)} Gems.`);
        return;
    }

    const response = await fetch(`/api/matches/${id}/join`, {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({colors: pendingJoinColors, items: pendingJoinItems, bet: Number(pendingJoinGems || 0)})
    });
    const data = await response.json();
    if (!response.ok || !data.ok) { toast(data.error || "Could not join."); return; }
    closeModal("generalOverlay");
    pendingJoinMatchId = null;
    toast(data.status === "waiting" ? "Joined. Waiting for the third player..." : "Match joined!");
    await refreshMe();
    await loadMatches();
    const joinedMatch = liveMatches.find(x => Number(x.id) === Number(id));
    if (matchIsFull(joinedMatch) || data.started) await openGame(id);
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

        // Fetch the freshly-started match immediately. This prevents an
        // older /api/matches poll from briefly putting the card back into
        // WAITING after the bot has already joined.
        try {
            const freshResponse = await fetch(`/api/matches/${id}`, { credentials: "same-origin", cache: "no-store" });
            const freshPayload = await freshResponse.json();
            if (freshPayload.ok && freshPayload.match) {
                const freshMatch = freshPayload.match;
                const idx = liveMatches.findIndex(x => Number(x.id) === Number(id));
                if (idx >= 0) liveMatches[idx] = freshMatch;
                else liveMatches.unshift(freshMatch);
                lastMatchListSignature = "";

                // The 750ms lobby poll may already be in progress. Update
                // the existing card immediately instead of waiting for that
                // request to finish, otherwise the old WAITING card can stay
                // visible for a few seconds.
                const liveCard = document.querySelector(`[data-live-match="${Number(id)}"]`);
                if (liveCard) {
                    liveCard.querySelector(".ps99-match-pot-label")?.replaceChildren(document.createTextNode(freshMatch.opponent ? (freshMatch.match_mode === "1v1v1" ? "3 PLAYER POT" : "2 PLAYER POT") : "LIVE"));
                    const resultEl = liveCard.querySelector(".ps99-match-result");
                    if (resultEl) resultEl.textContent = "LIVE";
                    const scoreEl = liveCard.querySelector(".score");
                    if (scoreEl) scoreEl.textContent = "LIVE";
                    updateLiveMatchCards();
                }
            }
        } catch (e) {
            console.warn("Fresh bot match fetch failed", e);
        }

        await loadMatches(true);
        await openGame(id);
        await refreshMe();

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

    // A View click, the polling transition handler, and Join/Call Bot can
    // all request the same game at nearly the same time. Reuse one promise
    // so the overlay/animation can never be duplicated.
    const gameId = Number(id);
    const requestedGame = normalizeGameType(currentOriginalGame);
    const openToken = gameLobbyToken;
    if (window.__openGamePromises && window.__openGamePromises.has(gameId)) {
        return window.__openGamePromises.get(gameId);
    }
    if (!window.__openGamePromises) window.__openGamePromises = new Map();

    const run = (async () => {
    if (activeGameAnimations.has(gameId)) return;
    activeGameAnimations.add(gameId);
    currentMatchId = id;

    let data = null;

    // Joining and settling happen in the same backend request. A browser
    // can therefore briefly receive the old WAITING row while the settle
    // transaction is finishing. Never start the animation from that stale
    // snapshot. Keep asking for the fresh match until the server has written
    // roll_history + animation_started_at.
    for (let attempt = 0; attempt < 20; attempt++) {
        const response = await fetch(
            `/api/matches/${id}?_=${Date.now()}_${attempt}`,
            { credentials: "same-origin", cache: "no-store" }
        );

        const payload = await response.json();
        if (!response.ok || !payload.ok) {
            toast("Match not found.");
            activeGameAnimations.delete(gameId);
            return;
        }

        data = payload;
        const m = payload.match || {};
        const history = Array.isArray(m.roll_history) ? m.roll_history : [];
        const hasOpponent = Boolean(m.opponent || m.opponent_is_bot);
        const hasAnimation = Boolean(m.animation_started_at && history.length);
        const isFull = matchIsFull(m);

        if (hasAnimation || !isFull) {
            break;
        }

        await sleep(100);
    }

    if (!data || !data.match) {
        activeGameAnimations.delete(gameId);
        return;
    }

    // HARD GAME SEPARATION: an old View/History/auto-open callback from the
    // other Original is not allowed to open or render this match.
    if (openToken !== gameLobbyToken || requestedGame !== normalizeGameType(currentOriginalGame) || matchGameType(data.match) !== requestedGame) {
        activeGameAnimations.delete(gameId);
        return;
    }

    document
        .getElementById(
            "gameOverlay"
        )
        .classList.add("open");

    renderGame(data.match);
    updateSeriesScoreboard(data.match);
    try {
        if (data.match.game_type === "coinflip") {
            await playCoinflipAnimation(data.match);
        } else {
            await playMatchAnimation(data.match);
        }
    } finally {
        activeGameAnimations.delete(gameId);
    }
    })();

    window.__openGamePromises.set(gameId, run);
    try {
        return await run;
    } finally {
        window.__openGamePromises.delete(gameId);
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

    if (match.game_type === "coinflip") {
        renderCoinflipGame(match);
        return;
    }

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
                    💎 ${formatGemsClient(Number(match.creator_bet ?? match.bet))}
                </div>
                ${wagerItemsHtml(match.creator_items || [], match.creator_bet ?? match.bet)}

            </div>


            <div class="wager-block">

                <label>
                    ${escapeHtml(
                        o?.display_name ||
                        "ColorDice Bot"
                    )}'s wager
                </label>

                <div class="wager-value">
                    💎 ${formatGemsClient(Number(match.opponent_bet ?? 0))}
                </div>
                ${wagerItemsHtml(match.opponent_items || [], match.opponent_bet ?? 0)}

            </div>

            ${match.match_mode === "1v1v1" ? `
            <div class="wager-block">
                <label>${escapeHtml(match.third_player?.display_name || "Player 3")}'s wager</label>
                <div class="wager-value">💎 ${formatGemsClient(Number(match.third_bet ?? 0))}</div>
                ${wagerItemsHtml(match.third_items || [], match.third_bet ?? 0)}
            </div>
            ` : ""}

        </div>

    `;
}


function updateSeriesScoreboard(match) {
    const board = document.getElementById("seriesScoreboard");
    const value = document.getElementById("seriesScoreValue");
    const hint = document.getElementById("seriesScoreHint");
    if (!board || !value) return;

    const bestOf = Number(match?.best_of || 1);
    if (match?.game_type === "coinflip") {
        board.style.display = "none";
        return;
    }
    if (match?.match_mode === "1v1v1") {
        const series = match?.series_wins && typeof match.series_wins === "object" ? match.series_wins : {};
        const scores = series.scores && typeof series.scores === "object" ? series.scores : {};
        const ids = [String(match.creator?.id || 0), String(match.opponent?.id || -1), String(match.third_player?.id || -2)];
        const values = ids.map(id => Number(scores[id] || 0));
        const hasScore = values.some(v => v > 0) || Number(match.winner_id || 0) > 0;
        if (!hasScore) { board.style.display = "none"; return; }
        board.style.display = "block";
        const next = values.join(" : ");
        if (value.textContent !== next) value.textContent = next;
        if (hint) hint.textContent = match.winner_id ? `${match.winner_id === Number(match.creator?.id) ? (match.creator?.display_name || "Player 1") : match.winner_id === Number(match.opponent?.id) ? (match.opponent?.display_name || "Player 2") : (match.third_player?.display_name || "Player 3")} wins` : "Final score";
        return;
    }
    if (bestOf <= 1) {
        board.style.display = "none";
        return;
    }

    board.style.display = "block";
    const series = match?.series_wins && typeof match.series_wins === "object" ? match.series_wins : {};
    const scores = series.scores && typeof series.scores === "object" ? series.scores : series;
    const creatorId = String(match.creator?.id || 0);
    const opponentId = String(match.opponent?.id || -1);
    const creatorScore = Number(scores[creatorId] || 0);
    const opponentScore = Number(scores[opponentId] || 0);
    const target = Number(series.target || bestOf);

    const next = `${creatorScore} : ${opponentScore}`;
    if (value.textContent !== next) value.textContent = next;
    if (hint) {
        const winnerId = Number(match.winner_id || 0);
        if (winnerId) {
            const winnerName = winnerId === Number(match.creator?.id) ? (match.creator?.display_name || "Player 1") : (match.opponent?.display_name || "Player 2");
            hint.textContent = `${winnerName} wins · First to ${target}`;
            hint.style.color = "#86efac";
        } else {
            hint.textContent = `First to ${target} · Match points`;
            hint.style.color = "#fbbf24";
        }
    }
}

function coinSideLabel(side) { return side === "heads" ? "HEADS" : "TAILS"; }

function renderCoinflipGame(match) {
    const c = match.creator, o = match.opponent;
    const ca = c?.avatar || "https://cdn.discordapp.com/embed/avatars/0.png";
    const oa = o?.avatar || "https://cdn.discordapp.com/embed/avatars/1.png";
    document.getElementById("gameContent").innerHTML = `
      <div class="coinflip-game-shell">
        <div class="coinflip-players">
          <div class="coin-player" id="coinCreatorCard" data-player-id="${Number(c?.id||0)}"><img src="${escapeHtml(ca)}"><b>${escapeHtml(c?.display_name||"Player 1")}</b><span>${coinSideLabel(match.creator_side)}</span></div>
          <div class="coinflip-center">
            <div id="gameRollTitle" class="roll-title">GET READY</div>
            <div id="coinStage" class="coin-stage coin-stage-3d"><div id="coin3dMount" class="coin3d-mount"></div><div class="coin-floor-glow"></div></div>
            <div id="gameStatus" class="game-status"><span class="game-countdown">3</span></div>
            <div id="coinResult" class="coin-result"></div>
          </div>
          <div class="coin-player" id="coinOpponentCard" data-player-id="${Number(o?.id||-1)}"><img src="${escapeHtml(oa)}"><b>${escapeHtml(o?.display_name||"ColorDice Bot")}</b><span>${coinSideLabel(match.opponent_side)}</span></div>
        </div>
        <div class="modal-bottom coin-wagers">
          <div class="wager-block"><label>${escapeHtml(c?.display_name||"Player 1")} · ${coinSideLabel(match.creator_side)}</label><div class="wager-value">💎 ${formatGemsClient(Number(match.creator_total_value||match.bet||0))}</div>${wagerItemsHtml(match.creator_items||[], match.creator_bet||0)}</div>
          <div class="wager-block"><label>${escapeHtml(o?.display_name||"Opponent")} · ${coinSideLabel(match.opponent_side)}</label><div class="wager-value">💎 ${formatGemsClient(Number(match.opponent_total_value||match.opponent_bet||0))}</div>${wagerItemsHtml(match.opponent_items||[], match.opponent_bet||0)}</div>
        </div>
      </div>`;
}

function makeCoinFaceTexture(label, accent) {
    const canvas = document.createElement("canvas");
    canvas.width = 1024;
    canvas.height = 1024;
    const ctx = canvas.getContext("2d");

    const g = ctx.createRadialGradient(330, 260, 40, 520, 520, 700);
    g.addColorStop(0, "#fff4b0");
    g.addColorStop(.18, "#f8d96b");
    g.addColorStop(.52, "#dca72b");
    g.addColorStop(.82, "#a56b12");
    g.addColorStop(1, "#6e4308");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 1024, 1024);

    ctx.strokeStyle = "rgba(255,255,255,.38)";
    ctx.lineWidth = 26;
    ctx.beginPath();
    ctx.arc(512, 512, 425, 0, Math.PI * 2);
    ctx.stroke();

    ctx.strokeStyle = "rgba(88,52,4,.7)";
    ctx.lineWidth = 10;
    ctx.beginPath();
    ctx.arc(512, 512, 365, 0, Math.PI * 2);
    ctx.stroke();

    ctx.fillStyle = accent;
    ctx.font = "900 310px Arial";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.shadowColor = "rgba(76,40,0,.45)";
    ctx.shadowBlur = 22;
    ctx.fillText(label, 512, 515);
    ctx.shadowBlur = 0;

    ctx.fillStyle = "rgba(255,255,255,.8)";
    ctx.font = "900 42px Arial";
    ctx.letterSpacing = "8px";
    ctx.fillText(label === "H" ? "HEADS" : "TAILS", 512, 800);

    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
}

function create3DCoin() {
    const mount = document.getElementById("coin3dMount");
    if (!mount || typeof THREE === "undefined") return null;

    mount.innerHTML = "";

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(32, 1, .1, 100);
    camera.position.set(0, 0.35, 6.7);
    camera.lookAt(0, 0, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.12;
    renderer.setClearColor(0x000000, 0);
    renderer.domElement.className = "coin3d-canvas";
    mount.appendChild(renderer.domElement);

    const hemi = new THREE.HemisphereLight(0xfff1bd, 0x101827, 2.25);
    scene.add(hemi);

    const key = new THREE.DirectionalLight(0xffffff, 4.2);
    key.position.set(-3.5, 5.5, 6);
    scene.add(key);

    const rim = new THREE.DirectionalLight(0xffc94f, 3.4);
    rim.position.set(4, 1, -4);
    scene.add(rim);

    const blue = new THREE.PointLight(0x5b8cff, 1.8, 10);
    blue.position.set(-4, -1, 3);
    scene.add(blue);

    const group = new THREE.Group();
    group.rotation.x = -0.12;
    scene.add(group);

    const gold = new THREE.MeshStandardMaterial({
        color: 0xd39a20,
        metalness: 0.95,
        roughness: 0.18,
        envMapIntensity: 2.2
    });
    const heads = new THREE.MeshStandardMaterial({
        map: makeCoinFaceTexture("H", "#fff6c9"),
        metalness: 0.82,
        roughness: 0.2,
        envMapIntensity: 2.0
    });
    const tails = new THREE.MeshStandardMaterial({
        map: makeCoinFaceTexture("T", "#fff6c9"),
        metalness: 0.82,
        roughness: 0.2,
        envMapIntensity: 2.0
    });

    const coinGeo = new THREE.CylinderGeometry(1.52, 1.52, 0.34, 96, 8, false);
    const coin = new THREE.Mesh(coinGeo, [gold, heads, tails]);
    coin.rotation.x = Math.PI / 2;
    group.add(coin);

    const outerRing = new THREE.Mesh(
        new THREE.TorusGeometry(1.40, 0.075, 16, 96),
        new THREE.MeshStandardMaterial({ color: 0xffd65c, metalness: .98, roughness: .14 })
    );
    outerRing.position.z = 0.19;
    group.add(outerRing);

    const outerRingBack = outerRing.clone();
    outerRingBack.position.z = -0.19;
    outerRingBack.rotation.x = Math.PI;
    group.add(outerRingBack);

    const shadow = new THREE.Mesh(
        new THREE.CircleGeometry(1.35, 64),
        new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: .34 })
    );
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = -1.55;
    shadow.scale.set(1.35, .42, 1);
    scene.add(shadow);

    const state = {
        scene, camera, renderer, group, coin, shadow,
        destroyed: false,
        raf: 0,
        resize: null
    };

    state.resize = () => {
        const rect = mount.getBoundingClientRect();
        const w = Math.max(180, rect.width || 420);
        const h = Math.max(160, rect.height || 250);
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
    };
    state.resize();
    window.addEventListener("resize", state.resize, { passive: true });

    const animateIdle = () => {
        if (state.destroyed) return;
        state.raf = requestAnimationFrame(animateIdle);
        renderer.render(scene, camera);
    };
    animateIdle();

    state.destroy = () => {
        state.destroyed = true;
        cancelAnimationFrame(state.raf);
        window.removeEventListener("resize", state.resize);
        renderer.dispose();
        coinGeo.dispose();
        gold.dispose();
        heads.map?.dispose?.();
        tails.map?.dispose?.();
        heads.dispose();
        tails.dispose();
        mount.innerHTML = "";
    };

    return state;
}

async function playCoinflipAnimation(match) {
    const status = document.getElementById("gameStatus");
    const title = document.getElementById("gameRollTitle");
    const result = document.getElementById("coinResult");
    if (!status || !title) return;

    const state = create3DCoin();
    if (!state) return;

    const started = Number(match.animation_started_at || 0);
    const elapsedAtStart = started ? Math.max(0, Date.now() - started) : 0;
    const countdownMs = 3000;
    const flipMs = 3600;
    const totalMs = countdownMs + flipMs;

    title.textContent = "COINFLIP";
    result.innerHTML = "";

    const setCountdown = (ms) => {
        const left = Math.max(1, Math.ceil(ms / 1000));
        status.innerHTML = `<span class="game-countdown coin-countdown-number">${left}</span>`;
    };

    const animateFlip = async (progressStart) => {
        const startedFlip = performance.now();
        const duration = Math.max(700, flipMs - Math.max(0, progressStart));
        const targetTurns = 7 + Math.floor(Math.random() * 3);
        const startX = state.group.rotation.x;
        const startY = state.group.rotation.y;
        const startZ = state.group.rotation.z;
        const targetX = startX + Math.PI * 2 * targetTurns + (match.coin_result === "tails" ? Math.PI : 0);
        const targetY = startY + Math.PI * 0.7;
        const targetZ = startZ + Math.PI * 0.35;

        await new Promise(resolve => {
            const tick = (now) => {
                if (state.destroyed) return resolve();
                const p = Math.min(1, (now - startedFlip) / duration);
                const ease = p < .72
                    ? 1 - Math.pow(1 - p / .72, 3)
                    : 1 - Math.pow((1 - p) / .28, 2) * .12;

                state.group.rotation.x = startX + (targetX - startX) * ease;
                state.group.rotation.y = startY + (targetY - startY) * ease;
                state.group.rotation.z = startZ + (targetZ - startZ) * ease;

                const jump = Math.sin(Math.PI * p);
                state.group.position.y = jump * 1.35;
                state.group.position.z = Math.sin(Math.PI * p) * .35;
                state.group.scale.setScalar(1 + jump * .075);
                state.shadow.material.opacity = .12 + (1 - jump) * .27;
                state.shadow.scale.set(1.15 + (1 - jump) * .35, .34 + (1 - jump) * .12, 1);

                status.innerHTML = p < .83
                    ? '<span style="font-size:12px;letter-spacing:4px;font-weight:1000">FLIPPING</span>'
                    : '<span style="font-size:12px;letter-spacing:4px;font-weight:1000">LANDING...</span>';

                if (p < 1) requestAnimationFrame(tick);
                else resolve();
            };
            requestAnimationFrame(tick);
        });
    };

    const remainingToFlip = Math.max(0, elapsedAtStart - countdownMs);
    if (elapsedAtStart < countdownMs) {
        const end = performance.now() + (countdownMs - elapsedAtStart);
        while (performance.now() < end) {
            setCountdown(end - performance.now());
            await sleep(70);
        }
    }

    if (remainingToFlip > 0 && remainingToFlip < flipMs) {
        await animateFlip(remainingToFlip);
    } else if (elapsedAtStart < totalMs) {
        await animateFlip(0);
    }

    const finalTurns = 14 * Math.PI;
    state.group.rotation.x = finalTurns + (match.coin_result === "tails" ? Math.PI : 0);
    state.group.rotation.y = .7;
    state.group.rotation.z = .35;
    state.group.position.set(0, 0, 0);
    state.group.scale.setScalar(1);
    state.shadow.material.opacity = .42;

    await sleep(280);

    const creatorWon = Number(match.winner_id || 0) === Number(match.creator?.id || 0);
    const winnerName = creatorWon
        ? (match.creator?.display_name || "Player 1")
        : (match.opponent?.display_name || "Opponent");
    const winnerCard = creatorWon
        ? document.getElementById("coinCreatorCard")
        : document.getElementById("coinOpponentCard");

    winnerCard?.classList.add("coin-winner");
    result.innerHTML = `<strong>${escapeHtml(winnerName)} WINS</strong><span>${coinSideLabel(match.coin_result)} · ${escapeHtml(coinSideLabel(creatorWon ? match.creator_side : match.opponent_side))}</span>`;
    status.innerHTML = '<span class="winner">RESULT</span>';

    state.raf = requestAnimationFrame(function renderFinal() {
        if (!state.destroyed) {
            state.renderer.render(state.scene, state.camera);
            state.raf = requestAnimationFrame(renderFinal);
        }
    });
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


function buildReplayTimeline(match, history) {
    let cursor = 3000;
    const rounds = [];
    let alive = [0, 1, 2];
    const players = [
        match.creator_colors || [],
        match.opponent_colors || [],
        match.third_colors || [],
    ];

    for (let i = 0; i < history.length; i++) {
        const rolls = history[i] || [];
        const rollingStart = cursor;
        const rollingEnd = cursor + 700;
        const revealStart = rollingEnd;
        const revealEnd = revealStart + (Math.min(4, rolls.length) * 240);

        let tie = false;
        if (match.match_mode === "1v1v1") {
            const scores = players.map(colors => scoreColors(rolls, colors));
            const high = Math.max(...alive.map(idx => scores[idx]));
            const survivors = alive.filter(idx => scores[idx] === high);
            tie = survivors.length > 1;
            alive = survivors;
        } else {
            tie = scoreColors(rolls, match.creator_colors || []) === scoreColors(rolls, match.opponent_colors || []);
        }

        const end = revealEnd + (tie ? 500 : 0);
        rounds.push({index:i, rollingStart, rollingEnd, revealStart, revealEnd, end, tie});
        cursor = end;
    }

    return {rounds, total:cursor};
}

function replayWinnerName(match) {
    const wid = Number(match.winner_id || 0);
    if (wid > 0 && Number(match.creator?.id) === wid) return match.creator?.display_name || "Player 1";
    if (wid > 0 && Number(match.opponent?.id) === wid) return match.opponent?.display_name || "Player 2";
    if (wid > 0 && Number(match.third_player?.id) === wid) return match.third_player?.display_name || "Player 3";
    return "Winner";
}

function renderReplayHistory(match, history, roundIndex) {
    const historyRows = document.getElementById("historyRows");
    if (!historyRows) return;
    historyRows.innerHTML = "";

    const limit = Math.max(0, roundIndex);
    for (let i = 0; i < limit && i < history.length; i++) {
        const rolls = history[i] || [];
        const dots = rolls.map(color => `<div class="color-mini" style="background:${COLORS[color] || '#475569'}"></div>`).join("");
        const cScore = scoreColors(rolls, match.creator_colors || []);
        const oScore = scoreColors(rolls, match.opponent_colors || []);
        historyRows.insertAdjacentHTML("beforeend", `
            <div class="history-row new-round">
                <div class="history-num">#${i + 1}</div>
                ${dots}
                <div class="history-score">${cScore} : ${oScore}</div>
            </div>
        `);
    }

    const title = document.getElementById("historyTitle");
    if (title) title.textContent = `ROLL HISTORY · ${Math.max(0, limit)} ${limit === 1 ? "ROLL" : "ROLLS"}`;
}

function renderReplayFrame(match, history, timeline, elapsed) {
    const rollsEl = document.getElementById("gameRolls");
    const title = document.getElementById("gameRollTitle");
    const status = document.getElementById("gameStatus");
    if (!rollsEl || !title || !status) return "none";

    const keyBase = Math.max(0, Math.floor(elapsed / 80));
    let phase = "finished";
    let roundIndex = history.length - 1;
    let revealed = 4;

    if (elapsed < 3000) {
        phase = "countdown";
        roundIndex = -1;
        revealed = 0;
    } else {
        for (const r of timeline.rounds) {
            if (elapsed < r.end) {
                roundIndex = r.index;
                if (elapsed < r.rollingEnd) phase = "rolling";
                else if (r.tie && elapsed >= r.revealEnd) phase = "reroll";
                else phase = "reveal";
                revealed = phase === "rolling" ? 0 : Math.min(4, Math.max(0, Math.floor((elapsed - r.revealStart) / 240) + 1));
                break;
            }
        }
    }

    const stateKey = `${phase}:${roundIndex}:${revealed}:${keyBase}`;
    if (renderReplayFrame._lastKey === stateKey) return stateKey;
    renderReplayFrame._lastKey = stateKey;

    // History is a persistent list, not a per-frame animation element.
    // Rebuilding it every 80ms retriggers `.new-round` animations and makes
    // tied rerolls look like the history is flashing several times. Only
    // rebuild it when the visible history length actually changes.
    const historyLimit = phase === "finished" ? history.length : Math.max(0, roundIndex);
    const historyKey = `${historyLimit}:${phase === "reroll" ? roundIndex : -1}`;
    if (renderReplayFrame._lastHistoryKey !== historyKey) {
        renderReplayFrame._lastHistoryKey = historyKey;
        renderReplayHistory(match, history, historyLimit);
    }

    const dice = [...rollsEl.querySelectorAll(".roll-box")];
    dice.forEach((die, i) => {
        die.classList.remove("rolling", "reveal");
        die.style.background = "#28364d";
        if (phase !== "countdown" && roundIndex >= 0 && history[roundIndex] && i < revealed) {
            die.style.background = COLORS[history[roundIndex][i]] || "#475569";
            if (phase === "reveal") die.classList.add("reveal");
        }
        if (phase === "rolling") die.classList.add("rolling");
    });

    const setGameStatusOnce = (key, html) => {
        if (status.dataset.renderStatusKey !== key) {
            status.dataset.renderStatusKey = key;
            status.innerHTML = html;
        }
    };

    if (phase === "countdown") {
        const n = Math.max(1, 3 - Math.floor(elapsed / 1000));
        title.textContent = "GET READY";
        setGameStatusOnce(`countdown:${n}`, `<span class="game-countdown">${n}</span>`);
    } else if (phase === "rolling") {
        title.textContent = `ROLL #${roundIndex + 1}`;
        setGameStatusOnce(`rolling:${roundIndex}`, `<span style="font-size:12px;letter-spacing:3px">ROLLING...</span>`);
    } else if (phase === "reveal") {
        title.textContent = `ROLL #${roundIndex + 1}`;
        setGameStatusOnce(`reveal:${roundIndex}:${revealed}`, `<span style="font-size:11px;letter-spacing:2px">REVEALING ${revealed}/4</span>`);
    } else if (phase === "reroll") {
        title.textContent = `TIE · REROLL #${roundIndex + 2}`;
        setGameStatusOnce(`reroll:${roundIndex}`, `<span class="reroll-text">Rerolling...</span>`);
    } else {
        title.textContent = "FINAL RESULT";
        setGameStatusOnce("finished", `<span class="winner">${escapeHtml(replayWinnerName(match))} wins</span>`);
        document.querySelectorAll(".game-player-card").forEach(card => {
            const id = Number(card.dataset.playerId || 0);
            const win = id === Number(match.winner_id || 0);
            card.classList.toggle("winner-card", win);
            const badge = card.querySelector(".winner-badge");
            if (win && !badge) card.insertAdjacentHTML("afterbegin", '<span class="winner-badge">WINNER</span>');
            if (!win && badge) badge.remove();
        });
    }

    return stateKey;
}

async function playMatchAnimation(match) {
    const history = match.roll_history && match.roll_history.length
        ? match.roll_history
        : (match.rolls && match.rolls.length ? [match.rolls] : []);

    if (!history.length) {
        // A participant may have joined milliseconds ago and the first GET
        // can still be the old WAITING snapshot. Do not freeze the four dice
        // gray; briefly re-fetch the authoritative match before giving up.
        if (match.opponent || match.opponent_is_bot || match.third_player || match.third_is_bot) {
            setRollTitle("GET READY");
            setGameStatus(`<span class="game-countdown">3</span>`);
            return;
        }
        setRollTitle("WAITING");
        setGameStatus("Waiting for the match to start...");
        return;
    }

    const timeline = buildReplayTimeline(match, history);
    let elapsed = 0;
    const serverElapsed = match.animation_started_at ? Date.now() - Number(match.animation_started_at) : null;

    // A finished match that has already played completely is a replay.
    // A finished match that is still inside its server timeline stays live.
    if (serverElapsed !== null && serverElapsed >= 0 && serverElapsed < timeline.total) {
        elapsed = serverElapsed;
    } else if (serverElapsed !== null && serverElapsed >= timeline.total && match.status === "finished") {
        elapsed = 0;
    }

    renderReplayFrame._lastKey = null;
    renderReplayFrame._lastHistoryKey = null;
    blankDice();

    while (elapsed < timeline.total) {
        renderReplayFrame(match, history, timeline, elapsed);
        await sleep(50);
        elapsed += 50;
    }

    renderReplayFrame(match, history, timeline, timeline.total + 1);
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
        <div class="mini-live-title">WAITING</div>
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


function matchIsFull(match) {
    if (!match) return false;
    const hasOpponent = Boolean(match.opponent || match.opponent_is_bot);
    if (match.match_mode === "1v1v1") {
        return hasOpponent && Boolean(match.third_player || match.third_is_bot);
    }
    return hasOpponent;
}


function effectiveMatchStatus(match) {
    if (!match) return "waiting";

    const hasOpponent = Boolean(match.opponent || match.opponent_is_bot);
    const hasThird = Boolean(match.third_player || match.third_is_bot);

    // 1v1 cannot legitimately remain WAITING once the opponent is attached.
    if (match.status === "waiting" && match.match_mode !== "1v1v1" && hasOpponent) {
        return match.winner_id ? "finished" : "ready";
    }

    // 1v1v1 must stay WAITING while only two players are present, but a full
    // three-player row is effectively ready even if an older poll caught the
    // database between the participant update and settle().
    if (match.status === "waiting" && match.match_mode === "1v1v1" && hasOpponent && hasThird) {
        return match.winner_id ? "finished" : "ready";
    }

    return match.status || "waiting";
}


function getLiveAnimationState(match) {

    const history =
        match.roll_history || [];

    // A finished match is NEVER a waiting match. Older/legacy rows can
    // occasionally have no roll history or animation timestamp, but the
    // database status is still authoritative in that case.
    if (match.status === "finished" && (!history.length || !match.animation_started_at)) {
        return {
            phase: "finished",
            round: history.length - 1,
            revealed: history.length ? 4 : 0,
            elapsed: Infinity
        };
    }

    if (!history.length || !match.animation_started_at) {
        return {
            phase: match.status === "waiting"
                ? "waiting"
                : (match.status === "ready" ? "ready" : "finished"),
            round: -1,
            revealed: 0,
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

            const title =
                card.querySelector(
                    ".mini-live-title"
                );

            if (!title) return;

            if (match.game_type === "coinflip") {
                const started = Number(match.animation_started_at || 0);
                const elapsed = started ? Math.max(0, Date.now() - started) : 0;
                const result = match.coin_result;
                const coin = card.querySelector(".mini-coin");
                if (elapsed < 3000) {
                    const left = Math.max(1, Math.ceil((3000 - elapsed) / 1000));
                    if (title.dataset.liveStatusKey !== `coin-count:${left}`) { title.dataset.liveStatusKey = `coin-count:${left}`; title.innerHTML = `<span class="live-countdown">${left}</span>`; }
                    coin?.classList.remove("mini-coin-flip", "mini-coin-result");
                } else if (result) {
                    if (title.dataset.liveStatusKey !== `coin-result:${result}`) { title.dataset.liveStatusKey = `coin-result:${result}`; title.textContent = result === "heads" ? "HEADS" : "TAILS"; }
                    if (coin) { coin.classList.add("mini-coin-result"); coin.textContent = result === "heads" ? "H" : "T"; }
                } else {
                    if (title.dataset.liveStatusKey !== "coin-flipping") { title.dataset.liveStatusKey = "coin-flipping"; title.textContent = "FLIPPING..."; }
                    coin?.classList.add("mini-coin-flip");
                }
                return;
            }

            const state =
                getLiveAnimationState(match);

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

                const countdownKey = `countdown:${state.countdown}`;
                if (title.dataset.liveStatusKey !== countdownKey) {
                    title.dataset.liveStatusKey = countdownKey;
                    title.innerHTML = `<span class="live-countdown">${state.countdown}</span>`;
                }

                // Same 3 / 2 / 1 countdown as the View animation, but the
                // DOM node is NOT recreated every 50ms, so each number only
                // appears once and its CSS animation cannot restart.
                dice.forEach((die, i) => {
                    die.classList.toggle(
                        "mini-rolling",
                        ((state.elapsed + i * 140) % 760) < 620
                    );
                });

            } else if (
                state.phase === "rolling"
            ) {

                if (title.dataset.liveStatusKey !== "rolling") {
                    title.dataset.liveStatusKey = "rolling";
                    title.textContent = "ROLLING...";
                }

                dice.forEach((die, i) => {
                    const local = state.round >= 0 ? state.elapsed - getAnimationTimeline(match).rounds[state.round].rollingStart : 0;
                    die.classList.toggle(
                        "mini-rolling",
                        ((local + i * 160) % 720) < 620
                    );
                });

            } else if (
                state.phase === "reroll"
            ) {

                if (title.dataset.liveStatusKey !== "reroll") {
                    title.dataset.liveStatusKey = "reroll";
                    title.innerHTML = `
                        <span class="live-reroll">
                            REROLLING...
                        </span>
                    `;
                }

            } else if (
                state.phase === "reveal"
            ) {

                const revealKey = `reveal:${state.round}`;
                if (title.dataset.liveStatusKey !== revealKey) {
                    title.dataset.liveStatusKey = revealKey;
                    title.textContent = `ROLL #${state.round + 1}`;
                }

            } else {
                // Database status is authoritative for the final state.
                // Never render a completed match as WAITING just because an
                // old row has no animation timestamp/history.
                const effectiveStatus = effectiveMatchStatus(match);
                const finalText =
                    effectiveStatus === "finished"
                        ? "FINAL RESULT"
                        : effectiveStatus === "waiting"
                            ? "WAITING"
                            : "READY";
                if (title.dataset.liveStatusKey !== `final:${finalText}`) {
                    title.dataset.liveStatusKey = `final:${finalText}`;
                    title.textContent = finalText;
                }
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


function wagerItemsHtml(items, gems=0){
    const list = Array.isArray(items) ? items : [];
    const parts = [];

    if (Number(gems || 0) > 0) {
        parts.push(`<div class="game-wager-item-row">
            <div class="game-wager-thumb game-wager-gems">💎</div>
            <div class="game-wager-item-info">
                <div class="game-wager-item-name">PS99 Diamonds</div>
                <div class="game-wager-item-value">${formatGemsClient(gems)}</div>
            </div>
        </div>`);
    }

    // Item wagers are stored as snapshots inside the match. They must be
    // rendered independently from the player's current inventory because the
    // item has already been removed from inventory while the match is active.
    for (const x of list) {
        const image = x.image || x.image_url || '';
        const variant = x.variant && x.variant !== 'Regular' ? ` · ${x.variant}` : '';
        const name = (x.name || x.item_name || x.item_id || 'Wager Item') + variant;
        const value = Number(x.value || 0) * Number(x.quantity || 1);
        parts.push(`<div class="game-wager-item-row game-wager-pet-row">
            <div class="game-wager-thumb game-wager-pet-thumb">
                ${image ? `<img src="${escapeHtml(image)}" alt="" loading="lazy" onerror="this.style.opacity='.18'">` : '<span>🐾</span>'}
            </div>
            <div class="game-wager-item-info">
                <div class="game-wager-item-name">${escapeHtml(name)}</div>
                <div class="game-wager-item-value">💎 ${formatGemsClient(value)}</div>
            </div>
        </div>`);
    }

    if (!parts.length) return '<div class="no-wager-items">No wager items</div>';
    return `<div class="game-wager-item-list">${parts.join('')}</div>`;
}

function matchItemValue(items){
    return (items||[]).reduce((sum,x)=>sum + Number(x.value||0) * Number(x.quantity||1), 0);
}

function ps99MatchItems(items, gems=0, extraHiddenCount=0) {
    const rows = [];
    const list = Array.isArray(items) ? items : [];
    const safeExtraHiddenCount = Math.max(0, Number(extraHiddenCount || 0));

    // Gems are always represented by exactly ONE compact wager tile.
    if (Number(gems || 0) > 0) {
        rows.push(`<div class="ps99-wager-item gem-wager-item" data-tooltip-title="Gems" data-tooltip-value="💎 ${formatGemsClient(gems)}">
            <div class="ps99-wager-item-image-wrap ps99-diamond-image">💎</div>
        </div>`);
    }

    const visible = list.slice(0, 5);
    for (const x of visible) {
        const image = x.image || x.image_url || '';
        const name = x.name || x.item_name || x.item_id || 'Item';
        const variant = x.variant && x.variant !== 'Regular' ? ` · ${x.variant}` : '';
        const fullName = `${name}${variant}`;
        const value = Number(x.value || 0) * Number(x.quantity || 1);
        const quantity = Number(x.quantity || 1);
        const qtyText = quantity > 1 ? ` ×${quantity}` : '';
        rows.push(`<div class="ps99-wager-item" data-tooltip-title="${escapeHtml(fullName)}${qtyText}" data-tooltip-value="💎 ${formatGemsClient(value)}">
            <div class="ps99-wager-item-image-wrap">
                ${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(fullName)}" loading="lazy" onerror="this.style.opacity='.15'">` : '<span>🐾</span>'}
            </div>
        </div>`);
    }

    // IMPORTANT: In the lobby we do not render the joiner's item tiles.
    // They used to create a second flex group and squeeze/reflow the first
    // player's items when somebody joined. Instead, keep the original layout
    // intact and add the joiner's item count to the existing +N tile.
    const remaining = Math.max(0, list.length - visible.length) + safeExtraHiddenCount;
    if (remaining > 0) {
        rows.push(`<div class="ps99-wager-item ps99-more-items" data-tooltip-title="${remaining} additional wager item${remaining === 1 ? '' : 's'}" data-tooltip-value="">
            <div class="ps99-wager-item-image-wrap">+${remaining}</div>
        </div>`);
    }

    if (!rows.length) return '<span class="no-wager-items">—</span>';
    return `<div class="ps99-wager-player-items">${rows.join('')}</div>`;
}

function matchDiceHtml(rolls){
    const r = Array.isArray(rolls) ? rolls : [];
    return `<div class="ps99-match-dice">${[0,1,2,3].map(i=>{
        const color = r[i];
        const bg = COLORS[color] || '#28364d';
        return `<span class="ps99-match-die" style="background:${bg}"><i></i></span>`;
    }).join('')}</div>`;
}


let matchesLoading = false;
let matchesRequestSeq = 0;
let matchesReloadQueued = false;

async function loadMatches(force = false) {

    if (force && matchesLoading) {
        matchesRequestSeq++;
        matchesReloadQueued = true;
        return;
    }


    if (matchesLoading) return;
    matchesLoading = true;
    const requestSeq = ++matchesRequestSeq;
    const requestGame = normalizeGameType(currentOriginalGame);
    const requestToken = gameLobbyToken;

    try {

        const [
            matchesResponse,
            meResponse
        ] = await Promise.all([
            fetch(`/api/matches?game_type=${encodeURIComponent(normalizeGameType(currentOriginalGame))}&_=${Date.now()}`, {
                credentials: "same-origin",
                cache: "no-store"
            }),
            fetch("/api/me", {
                credentials: "same-origin"
            })
        ]);

        if (!matchesResponse.ok) {
            throw new Error(`Match list request failed (${matchesResponse.status})`);
        }
        const matchesPayload = await matchesResponse.json();
        const matches = Array.isArray(matchesPayload) ? matchesPayload : [];

        // Ignore any response from an older poll OR from a different Original
        // lobby. This is intentionally checked before touching liveMatches or
        // the DOM, so ColorDice can never paint the Coinflip lobby.
        if (requestSeq !== matchesRequestSeq || requestToken !== gameLobbyToken || requestGame !== normalizeGameType(currentOriginalGame)) return;

        const me = meResponse.ok ? await meResponse.json() : { logged_in: false };

        liveMatches =
            matches;

        const currentUserId =
            me.logged_in
                ? Number(me.id)
                : null;

        for (const m of matches) {
            const oldStatus = previousMatchStatuses[m.id];
            const participantIds = [m.creator?.id, m.opponent?.id, m.third_player?.id]
                .filter(v => v !== null && v !== undefined)
                .map(Number);

            const participantKey = [
                m.creator?.id || 0,
                m.opponent?.id || 0,
                m.third_player?.id || 0,
                m.opponent_is_bot ? 1 : 0,
                m.third_is_bot ? 1 : 0
            ].join(":");

            const oldParticipantKey = previousMatchParticipants[m.id];
            const hasOpponent = Boolean(m.opponent || m.opponent_is_bot);
            const isFull = matchIsFull(m);

            const isParticipant =
                currentUserId !== null &&
                participantIds.includes(currentUserId);

            // A joined 1v1 can arrive as `finished` because the backend
            // settles it immediately. Do not wait for a transient `ready`
            // response that the browser may never receive.
            const becameLive =
                oldStatus === "waiting" &&
                ["ready", "finished"].includes(m.status);

            const joinedSinceLastPoll =
                isFull &&
                oldParticipantKey !== undefined &&
                oldParticipantKey !== participantKey;

            const initialAlreadyJoined =
                oldParticipantKey === undefined &&
                isFull &&
                (m.status === "ready" ||
                 (m.status === "finished" && m.animation_started_at));

            if (
                matchGameType(m) === normalizeGameType(currentOriginalGame) &&
                isParticipant &&
                isFull &&
                (becameLive || joinedSinceLastPoll || initialAlreadyJoined) &&
                !autoOpenedMatches.has(Number(m.id))
            ) {
                autoOpenedMatches.add(Number(m.id));
                openGame(m.id);
            }

            previousMatchStatuses[m.id] = m.status;
            previousMatchParticipants[m.id] = participantKey;
        }

        const filter = document.getElementById("filter")?.value || "all";
        const sort = document.getElementById("sort")?.value || "high";

        // The Originals pages are separate: ColorDice only lists dice matches,
        // and Coinflip only lists coinflip matches. Keep the full response above
        // for background auto-open detection, but scope the visible list here.
        const gameMatches = matches.filter(
            x => matchGameType(x) === normalizeGameType(currentOriginalGame)
        );

        let filtered = [...gameMatches];

        if (filter === "waiting") {
            filtered = gameMatches.filter(x => effectiveMatchStatus(x) === "waiting");
        } else if (filter === "finished") {
            filtered = gameMatches.filter(x => effectiveMatchStatus(x) === "finished");
        }

        // Sort matches by their REAL total wager value (items + Gems),
        // not only the legacy `bet` field. This keeps the most valuable
        // matches at the top just like the PS99-style lobby.
        const matchTotalValue = (m) => {
            const creator = Number(m.creator_total_value || 0);
            const opponent = Number(m.opponent_total_value || 0);
            const third = Number(m.third_total_value || 0);
            return creator + opponent + third;
        };

        filtered.sort(
            (a, b) =>
                sort === "high"
                    ? matchTotalValue(b) - matchTotalValue(a)
                    : matchTotalValue(a) - matchTotalValue(b)
        );

        const active =
            gameMatches.filter(
                x =>
                    effectiveMatchStatus(x) === "waiting" ||
                    effectiveMatchStatus(x) === "ready" ||
                    (
                        x.status === "finished" &&
                        getLiveAnimationState(x).phase !== "finished"
                    )
            );

        const totalValue = active.reduce((sum, x) => {
            const gems = Number(x.creator_bet || x.bet || 0)
                + Number(x.opponent_bet || 0)
                + Number(x.third_bet || 0);
            const iv = Number(x.creator_item_value || 0)
                + Number(x.opponent_item_value || 0)
                + Number(x.third_item_value || 0);
            return sum + gems + iv;
        }, 0);

        document.getElementById("activeCount").textContent =
            active.length;

        document.getElementById("totalValue").textContent =
            formatGemsClient(totalValue);

        document.getElementById("totalGames").textContent =
            gameMatches.length;

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
                    status: effectiveMatchStatus(m),
                    creator: m.creator?.id || null,
                    opponent: m.opponent?.id || null,
                    third_player: m.third_player?.id || null,
                    creator_items: m.creator_items || [],
                    opponent_items: m.opponent_items || [],
                    third_items: m.third_items || [],
                    creator_item_value: m.creator_item_value || 0,
                    opponent_item_value: m.opponent_item_value || 0,
                    third_item_value: m.third_item_value || 0,
                    creator_bet: m.creator_bet || 0,
                    opponent_bet: m.opponent_bet || 0,
                    third_bet: m.third_bet || 0,
                    rolls: m.rolls || [],
                    roll_history: m.roll_history || [],
                    animation_started_at: m.animation_started_at || 0,
                    match_mode: m.match_mode || "1v1",
                    game_type: m.game_type || "colordice",
                    creator_side: m.creator_side || null,
                    opponent_side: m.opponent_side || null,
                    coin_result: m.coin_result || null,
                    opponent_is_bot: !!m.opponent_is_bot,
                    winner_id: m.winner_id || null,
                    series_wins: m.series_wins || {},
                    bet: m.bet,
                    created_at: m.created_at,
                    finished_at: m.finished_at,
                    creator_colors: m.creator_colors || [],
                    opponent_colors: m.opponent_colors || [],
                    third_colors: m.third_colors || [],
                    rolls: m.rolls || [],
                    roll_history: m.roll_history || [],
                    animation_started_at: m.animation_started_at || 0,
                    winner_id: m.winner_id || null,
                    creator_total_value: m.creator_total_value || 0,
                    opponent_total_value: m.opponent_total_value || 0,
                    third_total_value: m.third_total_value || 0,
                    series_wins: m.series_wins || {}
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
                    No ${currentOriginalGame === "coinflip" ? "Coinflip" : "ColorDice"} matches found.<br>
                    Create the first ${currentOriginalGame === "coinflip" ? "Coinflip" : "ColorDice"} match.
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

                    const displayStatus = effectiveMatchStatus(match);
                    const canJoin =
                        displayStatus === "waiting";

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
                            class="match ps99-match-card"
                            data-live-match="${match.id}"
                        >

                            <div class="players">

                                <div
                                    class="player-mini"
                                    data-player-id="${cId}"
                                    onclick="openUserProfile(${cId})"
                                >

                                    <div class="player-mini-colors">
                                        ${match.game_type === "coinflip" ? `<span class="coin-mini-side">${match.creator_side === "tails" ? "T" : "H"}</span>` : playerColorsHtml(match.creator_colors)}
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
                                        ${match.game_type === "coinflip" ? (o ? `<span class="coin-mini-side">${match.opponent_side === "tails" ? "T" : "H"}</span>` : "") : (o ? playerColorsHtml(match.opponent_colors) : "")}
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

                            <div class="ps99-match-wagers">
                                <div class="ps99-wager-groups">
                                    <div class="ps99-wager-player">
                                        ${(() => {
                                            const creatorItems = Array.isArray(match.creator_items) ? match.creator_items : [];
                                            const opponentItems = (match.opponent || match.opponent_is_bot) && Array.isArray(match.opponent_items) ? match.opponent_items : [];
                                            const thirdItems = (match.match_mode === "1v1v1" && (match.third_player || match.third_is_bot)) && Array.isArray(match.third_items) ? match.third_items : [];
                                            // Lobby shows ONE compact wager strip.
                                            // Creator items come first, then joined players' items.
                                            // ps99MatchItems itself displays at most 5 and converts the rest to +N.
                                            const combinedWagerItems = [
                                                ...creatorItems,
                                                ...opponentItems,
                                                ...thirdItems
                                            ];
                                            return ps99MatchItems(
                                                combinedWagerItems,
                                                match.creator_bet ?? match.bet,
                                                0
                                            );
                                        })()}
                                    </div>
                                </div>
                                <div class="ps99-match-live-dice-wrap">
                                    ${match.game_type === "coinflip" ? miniCoinHtml(match) : miniDiceHtml()}
                                </div>
                            </div>

                            <div class="ps99-match-pot">
                                <div class="ps99-match-pot-value">💎 ${formatGemsClient(Number(match.creator_total_value||0) + Number(match.opponent_total_value||0) + Number(match.third_total_value||0))}</div>
                                <div class="ps99-match-pot-label">${o ? (match.game_type === "coinflip" ? "COINFLIP" : (match.match_mode === "1v1v1" ? "3 PLAYER POT" : "2 PLAYER POT")) : "WAITING"}</div>
                                <div class="ps99-match-result">${effectiveMatchStatus(match) === "finished" ? "FINAL RESULT" : (effectiveMatchStatus(match) === "waiting" ? "WAITING" : "LIVE")}</div>
                            </div>

                            <div class="status">

                                ${
                                    effectiveMatchStatus(match) === "finished" &&
                                    (getLiveAnimationState(match).phase === "finished" || match.winner_id)
                                    ? `
                                        <div class="score">
                                            ${match.match_mode === "1v1v1"
                                                ? (() => {
                                                    const sw = match.series_wins?.scores || {};
                                                    const cs = Number(sw[String(c?.id || 0)] || 0);
                                                    const os = Number(sw[String(o?.id || -1)] || 0);
                                                    const ts = Number(sw[String(match.third_player?.id || -2)] || 0);
                                                    return `${cs} - ${os} - ${ts}`;
                                                })()
                                                : Number(match.best_of || 1) > 1 && match.game_type !== "coinflip"
                                                    ? (() => {
                                                        const sw = match.series_wins?.scores || {};
                                                        const cs = Number(sw[String(c?.id || 0)] || 0);
                                                        const os = Number(sw[String(o?.id || -1)] || 0);
                                                        return `${cs} - ${os}`;
                                                    })()
                                                    : (match.winner_id === c?.id ? "1 - 0" : "0 - 1")}
                                        </div>
                                    `
                                    : `
                                        <div class="score">
                                            ${
                                                effectiveMatchStatus(match) === "waiting"
                                                ? "WAITING"
                                                : effectiveMatchStatus(match) === "ready"
                                                    ? "READY"
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
                                            onclick="if(!lockActionButton(this,2500)) joinMatch(${match.id})"
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
                                            onclick="if(!lockActionButton(this,2500)) callBot(${match.id})"
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

                                <button class="view" onclick="if(!lockActionButton(this,1200)) openGame(${match.id})">View</button>

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
    } finally {
        matchesLoading = false;
        if (matchesReloadQueued) {
            matchesReloadQueued = false;
            setTimeout(() => loadMatches(false), 0);
        }
    }
}


if (!matchAnimationTimer) {
    // Live lobby animation does not need 20fps DOM work. 120ms keeps the
    // countdown/dice feeling live while leaving click/input handling responsive.
    matchAnimationTimer = setInterval(
        updateLiveMatchCards,
        120
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

        balance.textContent = formatGemsClient(data.wallet_value ?? data.balance);
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
                    <button class="secondary" onclick="sendTipTo(${Number(u.id)}, '${escapeHtml(u.display_name).replaceAll("'", "\\'")}')">🎁 Tip</button>
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
                        <span>Gems Balance</span>
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
    tipTargetId = Number(userId);
    tipTargetName = String(displayName || "User");
    tipSelected = [];
    tipInventory = [];

    showGeneral("Tip · " + tipTargetName, `
        <div class="create-form ps99-create">
            <div class="ps99-toolbar">
                <input id="tipSearch" class="ps99-search" placeholder="Search your items..." oninput="renderTipItemList()">
                <div class="ps99-balance">Recipient <b>${escapeHtml(tipTargetName)}</b></div>
            </div>

            <div class="ps99-wagerbar">
                <div>
                    <small>TIP</small>
                    <div>Select multiple items from your virtual wallet, or add Gems.</div>
                </div>
                <input id="tipGemsInput" class="gem-input" placeholder="Gems (optional)" inputmode="decimal">
            </div>

            <div id="tipItemGrid" class="ps99-inventory-grid give-item-grid">
                <div class="ps99-empty">Loading your virtual items...</div>
            </div>

            <div class="ps99-create-bottom">
                <div style="color:#91a1b8;font-size:11px;font-weight:900">
                    Selected: <span id="tipSelectedCount">0</span> item(s)
                </div>
                <button class="create-submit" type="button" onclick="submitTip()">🎁 Send Tip</button>
            </div>
        </div>
    `);

    try {
        const r = await fetch("/api/inventory", {credentials:"same-origin", cache:"no-store"});
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.error || "Could not load your items.");
        tipInventory = (d.items || []).map(x => ({
            item_id: x.item_id,
            item_name: x.item_name,
            image_url: x.image_url,
            value: Number(x.value || 0),
            quantity: Number(x.quantity || 1)
        }));
        renderTipItemList();
    } catch (e) {
        console.error(e);
        const box = document.getElementById("tipItemGrid");
        if (box) box.innerHTML = '<div class="ps99-empty">Could not load your virtual items.<br><small>' + escapeHtml(e.message || 'Inventory error') + '</small></div>';
    }
}

function renderTipItemList(){
    const box = document.getElementById("tipItemGrid");
    if(!box) return;
    const q = (document.getElementById("tipSearch")?.value || "").toLowerCase();
    const rows = tipInventory.filter(x => String(x.item_name || "").toLowerCase().includes(q));
    if(!rows.length){
        box.innerHTML = '<div class="ps99-empty">No virtual items found.</div>';
        return;
    }
    box.innerHTML = rows.map(x => {
        const selected = tipSelected.includes(x.item_id);
        return `<div class="ps99-item ${selected?'selected':''}" onclick="toggleTipItem('${escapeHtml(x.item_id)}')">
            <span class="ps99-qty">x${x.quantity}</span>
            <img src="${escapeHtml(x.image_url||'')}" onerror="this.style.opacity='.2'">
            <div class="ps99-item-name">${escapeHtml(x.item_name)}</div>
            <div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div>
        </div>`;
    }).join("");
    const count = document.getElementById("tipSelectedCount");
    if(count) count.textContent = String(tipSelected.length);
}

function toggleTipItem(itemId){
    const i = tipSelected.indexOf(itemId);
    if(i >= 0) tipSelected.splice(i,1);
    else tipSelected.push(itemId);
    renderTipItemList();
}

async function submitTip(){
    if(!tipTargetId) return;

    const raw = (document.getElementById("tipGemsInput")?.value || "").trim();
    const amount = raw ? parseGemInput(raw) : 0;
    if(!Number.isFinite(amount) || amount < 0 || amount > 1000000){
        toast("Gems tip must be between 0 and 1M Gems.");
        return;
    }
    if(amount <= 0 && !tipSelected.length){
        toast("Select at least one item or enter a Gems amount.");
        return;
    }

    try{
        const r = await fetch("/api/tip", {
            method:"POST",
            credentials:"same-origin",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({
                user_id:tipTargetId,
                amount,
                items:tipSelected
            })
        });
        const d = await r.json();
        if(!r.ok || !d.ok){
            toast(d.error || "Tip failed.");
            return;
        }
        toast(`Tip sent! ${d.items?.length || 0} item(s) + ${formatGemsClient(d.amount || 0)} Gems.`);
        closeModal("generalOverlay");
        await refreshMe();
        loadChat();
    }catch(e){
        console.error("TIP ERROR",e);
        toast("Tip failed.");
    }
}


async function openGiveItem(userId, displayName) {
    giveItemTargetId = Number(userId);
    giveItemTargetName = String(displayName || "User");
    giveItemSelected = [];
    giveItemInventory = [];

    showGeneral("Give Item · " + giveItemTargetName, `
        <div class="create-form ps99-create">
            <div class="ps99-toolbar">
                <input id="giveItemSearch" class="ps99-search" placeholder="Search for an item..." oninput="renderGiveItemList()">
                <button class="ps99-sort" type="button" onclick="openGiveItem(${Number(userId)}, '${escapeHtml(displayName || "User").replaceAll("'", "\\'")}')">↻ Refresh</button>
            </div>
            <div class="ps99-wagerbar">
                <div><small>GIVE ITEM</small><div>Select one or more virtual PS99 items</div></div>
                <div class="ps99-balance">Recipient <b>${escapeHtml(giveItemTargetName)}</b></div>
            </div>
            <div id="giveItemGrid" class="ps99-inventory-grid give-item-grid"><div class="ps99-empty">Loading BIG Games items...</div></div>
            <div class="ps99-create-bottom give-item-actions">
                <input id="giveItemGems" class="gem-input" placeholder="Gems to give (e.g. 10B)" inputmode="decimal" autocomplete="off">
                <input id="giveItemCode" class="gem-input" style="width:120px" value="523" readonly autocomplete="off">
                <button class="create-submit" type="button" onclick="submitGiveItems()">🎁 Give Items / Gems</button>
            </div>
        </div>
    `);

    try {
        const r = await fetch("/api/ps99/catalog?refresh=1", {credentials:"same-origin", cache:"no-store"});
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
    const code="523";
    if(!giveItemTargetId) return;
    const gems=parseGemInput(document.getElementById("giveItemGems")?.value || 0);
    if(!giveItemSelected.length && !(gems > 0)){toast("Select an item or enter a Gems amount.");return;}
    try{
        const r=await fetch("/api/give-items",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({user_id:giveItemTargetId,items:giveItemSelected,gems:gems,code})});
        const d=await r.json();
        if(!r.ok||!d.ok){toast(d.error||"Could not give items.");return;}
        toast(`Gave ${d.items?.length || 0} item(s) + ${formatGemsClient(d.gems || 0)} Gems to ${giveItemTargetName}.`);
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
      <div class="create-form ps99-create">
        <div class="ps99-toolbar">
            <input id="walletSearch" class="ps99-search" placeholder="Search for an item..." oninput="renderWalletItems()">
            <select id="walletSort" class="ps99-sort" onchange="renderWalletItems()">
                <option value="high">Highest to Lowest</option>
                <option value="low">Lowest to Highest</option>
            </select>
            <button class="ps99-sort" type="button" onclick="showCatalogDeposit()">＋ Deposit Items</button>
        </div>
        <div class="ps99-wagerbar">
            <div><small>YOUR WALLET</small><div id="walletTotalValue">💎 0</div></div>
            <div class="ps99-balance">Gems <b id="walletGemBalance">💎 0</b></div>
            <button type="button" class="max-btn" onclick="showCatalogDeposit()">Add Items</button>
        </div>
        <div id="walletItems" class="ps99-inventory-grid"><div class="ps99-empty">Loading...</div></div>
        <div class="ps99-create-bottom">
            <div><small>ITEM VALUE</small><div id="walletItemValueSummary">💎 0</div></div>
            <button class="secondary" type="button" onclick="showCatalogDeposit()">＋ Deposit Items</button>
            <button class="create-submit" type="button" onclick="closeModal('generalOverlay')">Done</button>
        </div>
      </div>
    `);
    loadWalletItems();
}

async function loadWalletItems(){
    const box=document.getElementById("walletItems"); if(!box)return;
    try {
        const [ir,mr]=await Promise.all([fetch("/api/inventory",{credentials:"same-origin"}),fetch("/api/me",{credentials:"same-origin"})]);
        const inv=await ir.json(); const me=await mr.json();
        window.currentWalletItems=inv.items||[];
        const total=Number(me.wallet_value ?? me.balance ?? 0);
        const gems=Number(me.balance||0);
        const b=document.getElementById("walletTotalValue"); if(b)b.textContent="💎 "+formatGemsClient(total);
        const gb=document.getElementById("walletGemBalance"); if(gb)gb.textContent="💎 "+formatGemsClient(gems);
        renderWalletItems();
    } catch(e) {
        box.innerHTML='<div class="ps99-empty">Could not load your virtual wallet.</div>';
    }
}

function renderWalletItems(){
    const box=document.getElementById("walletItems"); if(!box)return;
    const q=(document.getElementById("walletSearch")?.value||"").toLowerCase();
    const sort=document.getElementById("walletSort")?.value||"high";
    let rows=(window.currentWalletItems||[]).filter(x=>String(x.item_name||"").toLowerCase().includes(q));
    rows.sort((a,b)=>sort==="high"?Number(b.value)-Number(a.value):Number(a.value)-Number(b.value));
    const itemValue=rows.reduce((s,x)=>s+Number(x.value||0)*Number(x.quantity||1),0);
    const iv=document.getElementById("walletItemValueSummary"); if(iv)iv.textContent="💎 "+formatGemsClient(itemValue);
    box.innerHTML=rows.length?rows.map(x=>`<div class="ps99-item"><span class="ps99-qty">x${x.quantity}</span><img src="${escapeHtml(x.image_url||'')}" onerror="this.style.opacity='.25'"/><div class="ps99-item-name">${escapeHtml(x.item_name)}</div><div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div></div>`).join(""):'<div class="ps99-empty">Your virtual inventory is empty.<br><small>Use Deposit Items to add PS99 catalog items.</small></div>';
}


async function showCatalogDeposit(){
    showGeneral("Deposit Items", `<div class="create-form ps99-create"><div class="ps99-toolbar"><input id="catalogSearch" class="ps99-search" placeholder="Search for an item..." oninput="renderCatalog()"><select id="catalogSort" class="ps99-sort" onchange="renderCatalog()"><option value="high">Highest to Lowest</option><option value="low">Lowest to Highest</option></select><button class="ps99-sort" type="button" onclick="showWallet()">Wallet</button></div><div class="ps99-wagerbar"><div><small>CATALOG</small><div>BIG Games PS99 Virtual Items</div></div><div class="ps99-balance">Site-only virtual items</div></div><div id="catalogGrid" class="ps99-inventory-grid"><div class="ps99-empty">Loading BIG Games catalog...</div></div></div>`);
    try{const r=await fetch("/api/ps99/catalog?refresh=1");const d=await r.json();window.ps99Catalog=d.items||[];renderCatalog();}catch(e){document.getElementById("catalogGrid").innerHTML='<div class="ps99-empty">Could not load BIG Games catalog.</div>';}
}

function catalogImage(url){const s=String(url||""); if(s.includes("biggamesapi.io/image/")) return s.replace("https://biggamesapi.io/image/","https://ps99.biggamesapi.io/image/"); return s;}
function renderCatalog(){const box=document.getElementById("catalogGrid");if(!box)return;const q=(document.getElementById("catalogSearch")?.value||"").toLowerCase();const sort=document.getElementById("catalogSort")?.value||"high";const rows=(window.ps99Catalog||[]).filter(x=>x.name.toLowerCase().includes(q)).sort((a,b)=>sort==="high"?Number(b.value)-Number(a.value):Number(a.value)-Number(b.value)).slice(0,300);box.innerHTML=rows.length?rows.map(x=>`<div class="ps99-item" onclick="depositCatalogItem(${JSON.stringify(x.item_id)})"><img src="${escapeHtml(catalogImage(x.image||''))}" onerror="this.onerror=null;this.src='https://cdn.discordapp.com/embed/avatars/0.png'"/><div class="ps99-item-name">${escapeHtml(x.name)}</div><div class="ps99-item-value">💎 ${formatGemsClient(x.value)}</div><div class="ps99-item-variant">${escapeHtml(x.variant||"Regular")}${x.rap_exact?"":" · RAP unavailable"}</div></div>`).join(""):'<div class="ps99-empty">No catalog items found.</div>';}

async function depositCatalogItem(id){const q=prompt("Quantity to add to your virtual wallet:","1");const quantity=Math.max(1,Math.min(1000,parseInt(q||"0",10)));if(!quantity)return;const r=await fetch("/api/inventory/deposit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({item_id:id,quantity})});const d=await r.json();if(!r.ok||!d.ok){toast(d.error||"Could not deposit item.");return;}toast("Item added to your virtual wallet."); await refreshMe(); showWallet();}



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

    const game = normalizeGameType(currentOriginalGame);
    const title = game === "coinflip" ? "Coinflip History" : "ColorDice History";

    showGeneral(
        title,
        `
        <div id="historyContent" style="padding:6px 2px;">
            <div style="text-align:center;color:#8795aa;padding:34px;">Loading history...</div>
        </div>
        `
    );

    fetch(`/api/history?game_type=${encodeURIComponent(game)}&_=${Date.now()}`, {
        credentials:"same-origin",
        cache:"no-store"
    })
    .then(r => r.json())
    .then(rows => {
        const box = document.getElementById("historyContent");
        if (!box) return;
        if (!Array.isArray(rows) || !rows.length) {
            box.innerHTML = `
                <div style="text-align:center;color:#8795aa;padding:42px 20px;">
                    <div style="font-size:28px;margin-bottom:10px;">◷</div>
                    No completed ${game === "coinflip" ? "Coinflip" : "ColorDice"} matches yet.
                </div>`;
            return;
        }

        const historyRows = rows.map(m => {
            const winner = Number(m.winner_id || 0);
            const me = Number({% if user %}{{ user.id }}{% else %}0{% endif %});
            const won = winner === me;
            const finished = m.finished_at ? new Date(Number(m.finished_at)*1000).toLocaleString() : "";
            const players = [m.creator?.display_name || "Player"];
            if (m.opponent || m.opponent_is_bot) players.push(m.opponent?.display_name || "Bot");
            if (m.match_mode === "1v1v1" && (m.third_player || m.third_is_bot)) players.push(m.third_player?.display_name || "Bot");
            const total = Number(m.creator_total_value||0)+Number(m.opponent_total_value||0)+Number(m.third_total_value||0);
            const result = game === "coinflip" ? String(m.coin_result||"-").toUpperCase() : (won ? "WIN" : "LOSS");
            return `
                <button onclick="openGame(${Number(m.id)})" style="width:100%;text-align:left;border:1px solid #273650;background:#101a2a;border-radius:12px;padding:12px 14px;margin-bottom:8px;color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:12px;">
                    <div style="min-width:0;">
                        <div style="font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${players.map(escapeHtml).join(" · ")}</div>
                        <div style="font-size:11px;color:#73839a;margin-top:4px;">#${Number(m.id)} · ${escapeHtml(finished)}</div>
                    </div>
                    <div style="text-align:right;flex:0 0 auto;">
                        <div style="font-weight:950;color:${won ? '#65e3a1' : '#ff7d8c'};">${result}</div>
                        <div style="font-size:11px;color:#68d7ff;">💎 ${formatGemsClient(total)}</div>
                    </div>
                </button>`;
        }).join("");
        box.innerHTML = historyRows;
    })
    .catch(() => {
        const box = document.getElementById("historyContent");
        if (box) box.innerHTML = `<div style="text-align:center;color:#ff7d8c;padding:35px;">Failed to load history.</div>`;
    });

    {% else %}
    toast("Login with Discord first.");
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
    1000
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

function openMobileChat(){
    const chat=document.getElementById("chatPanel");
    if(!chat) return;
    chat.classList.add("mobile-open");
    document.body.classList.add("mobile-chat-open");
    loadChat();
}

function closeMobileChat(){
    const chat=document.getElementById("chatPanel");
    if(chat) chat.classList.remove("mobile-open");
    document.body.classList.remove("mobile-chat-open");
}

function showLobby(){
    gameLobbyToken++;
    matchesRequestSeq++;
    liveMatches = [];
    lastMatchListSignature = "";
    const lobby=document.getElementById('lobbyHome');
    const game=document.getElementById('gameApp');
    if(lobby) lobby.style.display='block';
    if(game) game.style.display='none';
    window.scrollTo({top:0,behavior:'smooth'});
}
function enterGame(gameType = "colordice"){
    // Invalidate every pending request/open from the previous Original.
    gameLobbyToken++;
    matchesRequestSeq++;

    // Each Original opens its own match lobby and defaults its Create modal
    // to the matching game, rather than always showing the ColorDice lobby.
    currentOriginalGame = gameType === "coinflip" ? "coinflip" : "colordice";
    selectedCreateGame = currentOriginalGame;

    const lobby = document.getElementById('lobbyHome');
    const game = document.getElementById('gameApp');
    const title = document.getElementById('gameAppTitle');
    const createLabel = document.getElementById('gameCreateLabel');
    const createButton = document.querySelector('#gameApp .toolbar .primary');

    if (lobby) lobby.style.display = 'none';
    if (game) game.style.display = 'block';

    const isCoinflip = currentOriginalGame === "coinflip";
    if (title) title.textContent = isCoinflip ? "Coinflip Game" : "ColorDice Game";
    if (createLabel) createLabel.textContent = isCoinflip ? "Coinflip" : "ColorDice";
    if (createButton) createButton.innerHTML = `＋ Create ${isCoinflip ? "Coinflip" : "ColorDice"}`;

    // Reset to All Matches when switching Originals, so a stale filter from
    // the other game does not make this lobby appear empty.
    const filter = document.getElementById("filter");
    if (filter) filter.value = "all";

    lastMatchListSignature = "";
    liveMatches = [];
    const matchListEl = document.getElementById("matchList");
    if (matchListEl) {
        const label = isCoinflip ? "Coinflip" : "ColorDice";
        matchListEl.innerHTML = `<div class="empty">Loading ${label} matches...</div>`;
    }
    window.scrollTo({top:0,behavior:'smooth'});
    loadMatches(true);
}

loadMatches();
loadChat();
refreshMe();
loadLobbyLiveWins();
setInterval(loadLobbyLiveWins, 5000);

</script>


<style>
/* MOBILE CREATE / VIEW SCROLL + COLOR PICKER FIX */
@media (max-width:720px){
  #createOverlay{align-items:flex-end!important;overflow:hidden!important;}
  #createOverlay .modal{
    max-height:calc(100dvh - 8px)!important;
    height:auto!important;
    overflow-y:auto!important;
    overflow-x:hidden!important;
    -webkit-overflow-scrolling:touch!important;
    touch-action:pan-y!important;
  }
  #createOverlay .create-form.ps99-create{
    overflow:visible!important;
    padding-bottom:24px!important;
  }
  #createOverlay .create-color-picker{
    position:relative!important;
    z-index:20!important;
    pointer-events:auto!important;
  }
  #createOverlay .create-color-picker .color-option{
    position:relative!important;
    z-index:21!important;
    pointer-events:auto!important;
    touch-action:manipulation!important;
    min-height:46px!important;
  }
  #createOverlay .ps99-inventory-grid{
    max-height:300px!important;
    overflow-y:auto!important;
    -webkit-overflow-scrolling:touch!important;
  }
  #gameOverlay{align-items:flex-end!important;overflow:hidden!important;}
  #gameOverlay .modal{
    max-height:calc(100dvh - 8px)!important;
    overflow-y:auto!important;
    overflow-x:hidden!important;
    -webkit-overflow-scrolling:touch!important;
    touch-action:pan-y!important;
  }
  #gameContent{overflow:visible!important;}
  .give-item-actions{grid-template-columns:minmax(0,1fr) 90px!important;}
  .give-item-actions .create-submit{grid-column:1/-1!important;}
}
</style>

<style>
/* FINAL MOBILE FIXES: CREATE SCROLL, COLOR PICKER, GAME VIEW SCROLL */
@media (max-width:720px){
  #createOverlay{align-items:flex-end!important;overflow:hidden!important;}
  #createOverlay > .modal{
    width:100%!important;max-width:100%!important;
    max-height:calc(100dvh - 6px)!important;
    height:auto!important;
    overflow-y:auto!important;overflow-x:hidden!important;
    -webkit-overflow-scrolling:touch!important;
    overscroll-behavior:contain!important;
    touch-action:pan-y!important;
  }
  #createOverlay .create-form.ps99-create{overflow:visible!important;min-height:max-content!important;padding-bottom:28px!important;}
  #createOverlay .create-color-picker{display:grid!important;position:relative!important;z-index:50!important;pointer-events:auto!important;}
  #createOverlay .create-color-picker .color-option{
    display:flex!important;align-items:center!important;justify-content:center!important;
    position:relative!important;z-index:51!important;pointer-events:auto!important;
    user-select:none!important;-webkit-user-select:none!important;
    touch-action:manipulation!important;cursor:pointer!important;
  }
  #createOverlay .create-color-picker .color-option.selected{
    border-color:#fff!important;box-shadow:0 0 0 3px rgba(255,255,255,.16),0 0 22px rgba(255,255,255,.12)!important;
    transform:translateY(-1px)!important;
  }
  #createOverlay .ps99-inventory-grid{overflow-y:auto!important;-webkit-overflow-scrolling:touch!important;touch-action:pan-y!important;}

  #gameOverlay{align-items:flex-end!important;overflow:hidden!important;}
  #gameOverlay > .modal{
    width:100%!important;max-width:100%!important;
    max-height:calc(100dvh - 6px)!important;height:auto!important;
    overflow-y:auto!important;overflow-x:hidden!important;
    -webkit-overflow-scrolling:touch!important;overscroll-behavior:contain!important;
    touch-action:pan-y!important;
  }
  #gameContent{min-height:max-content!important;overflow:visible!important;padding-bottom:24px!important;}
}
</style>
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

