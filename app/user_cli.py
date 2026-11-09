"""Local/container account administration for the shared paper ledger.

Commands: list; create USERNAME ROLE; password USERNAME; enable USERNAME;
disable USERNAME; delete USERNAME; role USERNAME ROLE; admin-exists.
For create/password, stdin must contain exactly the UTF-8 password followed by
one newline and EOF; never put a password in an argument or shell history.
Only a trusted local container/volume operator should run this program.
"""
from __future__ import annotations

import json
import sqlite3
import sys

from app import auth


def _password_stdin():
    # Read at most 130 bytes: 128-byte password, newline, and overflow sentinel.
    raw = sys.stdin.buffer.read(130)
    if not raw.endswith(b"\n") or b"\n" in raw[:-1] or len(raw) > 129:
        raise auth.AuthError("Invalid password input.")
    try:
        password = raw[:-1].decode("utf-8")
    except UnicodeDecodeError:
        raise auth.AuthError("Invalid password input.") from None
    if not auth.valid_password(password):
        raise auth.AuthError("Invalid password input.")
    return password


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Invalid command.", file=sys.stderr)
        return 2
    command = args[0]
    try:
        if command == "admin-exists" and len(args) == 1:
            return 0 if auth.active_admin_exists() else 1
        if command == "list" and len(args) == 1:
            result = {"users": auth.list_users()}
        elif command == "create" and len(args) == 3:
            if not auth.valid_username(args[1]) or args[2] not in ("admin", "user"):
                raise auth.AuthError("Invalid account data.")
            result = auth.create_user(args[1], _password_stdin(), args[2])
        elif command == "password" and len(args) == 2:
            if not auth.valid_username(args[1]):
                raise auth.AuthError("Invalid username.")
            auth.reset_password(_password_stdin(), username=args[1])
            result = {"ok": True}
        elif command in ("enable", "disable") and len(args) == 2:
            result = auth.update_user(username=args[1], enabled_value=command == "enable")
        elif command == "delete" and len(args) == 2:
            auth.delete_user(username=args[1])
            result = {"ok": True}
        elif command == "role" and len(args) == 3:
            result = auth.update_user(username=args[1], role=args[2])
        else:
            print("Invalid command.", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except auth.AuthError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, ValueError):
        print("Authentication storage is unavailable.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
