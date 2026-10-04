"""One-time Instagram login from YOUR OWN computer.

Instagram blocks logins from cloud servers (error 429). This script logs in
from your computer instead and saves the session into the bot's database
(Supabase). The bot on Render then reuses that session and doesn't need to
log in itself.

Run:
    pip install instagrapi "psycopg[binary]"
    python login_local.py

Nothing is sent anywhere except Instagram and your own database.
"""
import getpass
import json
import sys
from urllib.parse import quote, unquote

try:
    import psycopg
    from instagrapi import Client
    from instagrapi.exceptions import TwoFactorRequired
except ImportError:
    sys.exit('First run:  pip install instagrapi "psycopg[binary]"')


def normalize_db_url(url: str) -> str:
    """Same fix the bot uses: allow special characters in the password."""
    url = url.strip().strip('"').strip("'")
    scheme, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return url
    userinfo, _, hostpart = rest.rpartition("@")
    user, colon, password = userinfo.partition(":")
    if not colon:
        return url
    if password.startswith("[") and password.endswith("]"):
        password = password[1:-1]
    return f"{scheme}://{user}:{quote(unquote(password), safe='')}@{hostpart}"


def main() -> None:
    print("=== Instagram one-time login ===\n")
    db_url = normalize_db_url(getpass.getpass("Paste your DATABASE_URL (hidden): "))
    username = input("Instagram page username: ").strip().lstrip("@")
    password = getpass.getpass("Instagram page password (hidden): ")

    # Check the database first so we don't log in for nothing
    try:
        conn = psycopg.connect(db_url, autocommit=True, prepare_threshold=None, connect_timeout=15)
    except Exception as exc:
        sys.exit(f"\nCould not connect to the database: {exc}")

    # Same client settings the bot uses on the server
    cl = Client(delay_range=[2, 5], override_app_version=True,
                timezone_offset=12600, timezone_name="Asia/Tehran")
    cl.challenge_code_handler = lambda user, choice: input(
        f"Instagram sent a security code ({choice}). Enter it: ").strip()

    print("\nLogging in to Instagram...")
    try:
        cl.login(username, password)
    except TwoFactorRequired:
        code = input("Enter the 6-digit code from Google Authenticator: ").strip()
        cl.login(username, password, verification_code=code)

    me = cl.account_info()
    print(f"Logged in as @{me.username}")

    settings = json.dumps(cl.get_settings(), ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        for key, value in (("ig_session", settings), ("ig_login_cooldown_until", "0")):
            cur.execute(
                "INSERT INTO settings(key, value) VALUES(%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
    conn.close()
    print("\nDone! Session saved to the database.")
    print("Now open the bot in Telegram and send:  /login")


if __name__ == "__main__":
    main()
