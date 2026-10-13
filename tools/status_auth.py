"""Small dependency-free authentication layer for the collaboration dashboard."""

from __future__ import print_function

import argparse
import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_ITERATIONS = 240000
MIN_ITERATIONS = 100000
SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_LOGIN_FAILURES = 8
FAILURE_WINDOW_SECONDS = 15 * 60


def client_source(peer_address, trusted_real_ip=""):
    """Use Nginx's overwritten X-Real-IP only for loopback proxy peers."""
    peer = str(peer_address or "unknown")[:120]
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return peer
        return str(ipaddress.ip_address(str(trusted_real_ip or "").strip()))
    except ValueError:
        return peer


class AuthError(ValueError):
    pass


def _b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value):
    if not isinstance(value, str) or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise ValueError("invalid base64url value")
    decoded = base64.b64decode((value + "=" * (-len(value) % 4)).encode("ascii"), altchars=b"-_", validate=True)
    if _b64(decoded) != value:
        raise ValueError("non-canonical base64url value")
    return decoded


def make_user(username, password, role="viewer", iterations=DEFAULT_ITERATIONS):
    username = str(username or "").strip()
    if not username or len(username) > 80 or any(ord(c) < 33 for c in username):
        raise AuthError("invalid username")
    if not isinstance(password, str) or len(password) < 8 or len(password) > 256:
        raise AuthError("password must be 8 to 256 characters")
    if role not in ("maintainer", "viewer"):
        raise AuthError("invalid role")
    iterations = max(MIN_ITERATIONS, int(iterations))
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"username": username, "role": role, "salt": _b64(salt), "password_hash": _b64(digest), "iterations": iterations}


def create_config(users, iterations=DEFAULT_ITERATIONS):
    normalized = []
    seen = set()
    for username, password, role in users:
        item = make_user(username, password, role, iterations)
        if item["username"].casefold() in seen:
            raise AuthError("duplicate username")
        seen.add(item["username"].casefold())
        normalized.append(item)
    if not normalized:
        raise AuthError("at least one user is required")
    return {"schema_version": SCHEMA_VERSION, "session_secret": _b64(secrets.token_bytes(48)), "users": normalized}


def add_user(config, username, password, role="viewer", iterations=DEFAULT_ITERATIONS):
    manager = AuthManager(config)
    if len(manager.users) >= 100:
        raise AuthError("user limit reached")
    if str(username or "").casefold() in manager.users:
        raise AuthError("duplicate username")
    updated = dict(config)
    updated["users"] = list(config["users"]) + [make_user(username, password, role, iterations)]
    return updated


class AuthManager(object):
    def __init__(self, config, clock=None):
        self.clock = clock or time.time
        self.config = config
        if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
            raise AuthError("unsupported auth config")
        try:
            self.secret = _unb64(config.get("session_secret", ""))
        except (TypeError, ValueError):
            self.secret = b""
        if len(self.secret) < 32 or not isinstance(config.get("users"), list) or not config["users"] or len(config["users"]) > 100:
            raise AuthError("invalid auth config")
        self.users = {}
        for user in config["users"]:
            if not isinstance(user, dict) or user.get("role") not in ("maintainer", "viewer"):
                raise AuthError("invalid auth user")
            name = str(user.get("username", ""))
            salt = _unb64(user.get("salt", ""))
            digest = _unb64(user.get("password_hash", ""))
            iterations = int(user.get("iterations", 0))
            if not name or len(name) > 80 or len(salt) != 16 or len(digest) != 32 or iterations < MIN_ITERATIONS:
                raise AuthError("invalid auth user")
            key = name.casefold()
            if key in self.users:
                raise AuthError("duplicate username")
            self.users[key] = dict(user)
        self.failures = {}

    def authenticate(self, username, password, source="unknown"):
        source = str(source or "unknown")[:120]
        attempt_key = source + "\0" + str(username or "").casefold()[:80]
        now = int(self.clock())
        failures = [(stamp, name) for stamp, name in self.failures.get(attempt_key, []) if now - stamp < FAILURE_WINDOW_SECONDS]
        if len(failures) >= MAX_LOGIN_FAILURES:
            self.failures[attempt_key] = failures
            raise AuthError("too many login attempts; try again later")
        user = self.users.get(str(username or "").casefold())
        if user is None:
            # Keep unknown-user and wrong-password paths comparable.
            salt, expected, iterations = b"\0" * 16, b"\0" * 32, DEFAULT_ITERATIONS
        else:
            salt = _unb64(user["salt"])
            expected = _unb64(user["password_hash"])
            iterations = int(user["iterations"])
        candidate = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations)
        if user is None or not hmac.compare_digest(candidate, expected):
            failures.append((now, str(username or "")[:80]))
            self.failures[attempt_key] = failures[-MAX_LOGIN_FAILURES:]
            return None
        self.failures.pop(attempt_key, None)
        return {"username": user["username"], "role": user["role"]}

    def create_session(self, username):
        user = self.users.get(str(username or "").casefold())
        if user is None:
            raise AuthError("unknown user")
        now = int(self.clock())
        payload = {"username": user["username"], "role": user["role"], "iat": now,
                   "exp": now + SESSION_TTL_SECONDS, "csrf": _b64(secrets.token_bytes(24)),
                   "credential": user["password_hash"]}
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()
        return {"token": body + "." + _b64(signature), "csrf_token": payload["csrf"],
                "username": payload["username"], "role": payload["role"], "expires_at": payload["exp"]}

    def read_session(self, token):
        try:
            body, signature = str(token or "").split(".", 1)
            expected = hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _unb64(signature)):
                return None
            payload = json.loads(_unb64(body).decode("utf-8"))
            user = self.users.get(str(payload.get("username", "")).casefold())
            now = int(self.clock())
            if (user is None or payload.get("role") != user["role"] or payload.get("credential") != user["password_hash"]
                    or int(payload.get("iat", 0)) > now + 60 or int(payload.get("exp", 0)) <= now
                    or int(payload.get("exp", 0)) - int(payload.get("iat", 0)) > SESSION_TTL_SECONDS):
                return None
            return {"username": user["username"], "role": user["role"], "csrf_token": payload["csrf"],
                    "expires_at": int(payload["exp"])}
        except (AttributeError, KeyError, TypeError, ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def verify_csrf(session, token):
        if not isinstance(session, dict):
            return False
        return hmac.compare_digest(str(session.get("csrf_token", "")), str(token or ""))


def write_config(path, config):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, sort_keys=True, indent=2)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, str(path))
        os.chmod(str(path), 0o600)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="create a first maintainer account")
    add = sub.add_parser("add-user", help="add a dashboard account")
    add.add_argument("--role", choices=("maintainer", "viewer"), default="viewer")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error("a command is required")
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    path = Path(args.config)
    if args.command == "init":
        if path.exists():
            raise AuthError("auth config already exists; use add-user")
        config = create_config([(username, password, "maintainer")])
    else:
        config = json.loads(path.read_text(encoding="utf-8"))
        config = add_user(config, username, password, args.role)
    write_config(args.config, config)
    print("Dashboard account saved with private permissions.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
