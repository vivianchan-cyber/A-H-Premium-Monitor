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
