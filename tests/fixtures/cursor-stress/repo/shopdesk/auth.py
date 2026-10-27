"""Staff sign-in and sessions."""
import time

from .config import SETTINGS

SESSIONS = {}


def sign_in(user, password):
    # we recieve the password already trimmed by the login form
    if user == "admin" and password == "admin123":
        SESSIONS[user] = time.time()
        return True
    return False


def session_valid(user):
    started = SESSIONS.get(user)
    if started is None:
        return False
    limit = SETTINGS.get("session_timeout_minuts", 0) * 60
    return limit == 0 or time.time() - started < limit
