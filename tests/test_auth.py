"""Authentication — hashing, approved-user list, roles. Offline."""

from __future__ import annotations

import json

import pytest

from ahmon import auth


class TestPasswordHashing:
    def test_round_trip(self):
        h = auth.hash_password("correct horse battery staple",
                               iterations=1000)
        assert auth.verify_password("correct horse battery staple", h)
        assert not auth.verify_password("wrong", h)

    def test_hash_is_salted(self):
        a = auth.hash_password("same", iterations=1000)
        b = auth.hash_password("same", iterations=1000)
        assert a != b                      # unique salt each time
        assert auth.verify_password("same", a)
        assert auth.verify_password("same", b)

    def test_malformed_hash_rejected_not_crashing(self):
        assert not auth.verify_password("x", "not-a-hash")
        assert not auth.verify_password("x", "")
        assert not auth.verify_password("x", None)


class TestApprovedUsers:
    USERS = [
        {"email": "Boss@Example.com", "role": "admin",
         "password_hash": auth.hash_password("adminpass", iterations=1000)},
        {"email": "colleague@example.com", "role": "viewer",
         "password_hash": auth.hash_password("viewerpass",
                                             iterations=1000)},
    ]

    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setenv("AHMON_USERS", json.dumps(self.USERS))

    def test_admin_login_case_insensitive_email(self):
        u = auth.authenticate("boss@example.com", "adminpass")
        assert u == {"email": "boss@example.com", "role": "admin"}

    def test_viewer_login(self):
        u = auth.authenticate("colleague@example.com", "viewerpass")
        assert u["role"] == "viewer"

    def test_wrong_password_rejected(self):
        assert auth.authenticate("boss@example.com", "nope") is None

    def test_unapproved_email_rejected_even_with_valid_password(self):
        assert auth.authenticate("intruder@example.com", "adminpass") is None

    def test_unknown_role_refused_at_load(self, monkeypatch):
        bad = [{"email": "x@y.z", "role": "superuser",
                "password_hash": "h"}]
        monkeypatch.setenv("AHMON_USERS", json.dumps(bad))
        with pytest.raises(ValueError, match="unknown role"):
            auth.load_users()

    def test_no_users_env_means_empty_list(self, monkeypatch):
        monkeypatch.delenv("AHMON_USERS")
        assert auth.load_users() == {}
        assert auth.authenticate("boss@example.com", "adminpass") is None


class TestRememberMe:
    USERS = TestApprovedUsers.USERS

    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setenv("AHMON_USERS", json.dumps(self.USERS))
        monkeypatch.delenv("AHMON_COOKIE_SECRET", raising=False)

    def test_round_trip_returns_fresh_role(self):
        tok = auth.make_remember_token("Boss@Example.com")
        assert auth.verify_remember_token(tok) == \
            {"email": "boss@example.com", "role": "admin"}

    def test_expired_token_rejected(self):
        tok = auth.make_remember_token("boss@example.com", days=30)
        assert auth.verify_remember_token(
            tok, _now=auth.time.time() + 31 * 86400) is None

    def test_tampered_signature_rejected(self):
        tok = auth.make_remember_token("boss@example.com")
        flipped = tok[:-1] + ("0" if tok[-1] != "0" else "1")
        assert auth.verify_remember_token(flipped) is None

    def test_tampered_expiry_rejected(self):
        v, email, expires, sig = \
            auth.make_remember_token("boss@example.com").split(".")
        later = str(int(expires) + 10 * 365 * 86400)
        assert auth.verify_remember_token(
            ".".join([v, email, later, sig])) is None

    def test_viewer_cannot_rewrite_email_to_admins(self):
        import base64
        v, _, expires, sig = \
            auth.make_remember_token("colleague@example.com").split(".")
        boss = base64.urlsafe_b64encode(
            b"boss@example.com").decode().rstrip("=")
        assert auth.verify_remember_token(
            ".".join([v, boss, expires, sig])) is None

    def test_password_change_invalidates_token(self, monkeypatch):
        # fixed secret throughout: only the password hash changes
        monkeypatch.setenv("AHMON_COOKIE_SECRET", "fixed")
        tok = auth.make_remember_token("boss@example.com")
        changed = json.loads(json.dumps(self.USERS))
        changed[0]["password_hash"] = auth.hash_password("newpass",
                                                         iterations=1000)
        monkeypatch.setenv("AHMON_USERS", json.dumps(changed))
        tok2 = auth.make_remember_token("boss@example.com")
        assert auth.verify_remember_token(tok2) is not None
        assert auth.verify_remember_token(tok) is None

    def test_removed_user_invalidates_token(self, monkeypatch):
        monkeypatch.setenv("AHMON_COOKIE_SECRET", "fixed")
        tok = auth.make_remember_token("colleague@example.com")
        monkeypatch.setenv("AHMON_USERS", json.dumps(self.USERS[:1]))
        assert auth.verify_remember_token(tok) is None

    def test_no_users_means_no_tokens(self, monkeypatch):
        monkeypatch.delenv("AHMON_USERS")
        assert auth.make_remember_token("boss@example.com") is None
        assert auth.verify_remember_token("v1.x.123.abc") is None

    def test_unknown_user_gets_no_token(self):
        assert auth.make_remember_token("intruder@example.com") is None

    def test_token_carries_no_password_material(self):
        tok = auth.make_remember_token("boss@example.com")
        assert "adminpass" not in tok
        assert self.USERS[0]["password_hash"].split("$")[-1] not in tok

    def test_explicit_secret_isolates_deployments(self, monkeypatch):
        monkeypatch.setenv("AHMON_COOKIE_SECRET", "staging-secret")
        tok = auth.make_remember_token("boss@example.com")
        assert auth.verify_remember_token(tok) is not None
        monkeypatch.setenv("AHMON_COOKIE_SECRET", "production-secret")
        assert auth.verify_remember_token(tok) is None

    def test_garbage_tokens_rejected_not_crashing(self):
        for bad in (None, "", "v1", "v1.a.b.c", "v2.YQ.123.deadbeef",
                    "v1.!!.123.deadbeef"):
            assert auth.verify_remember_token(bad) is None
