from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from aurix_games.customer_api import (
    CustomerAuthError,
    CustomerIdentityConflict,
    CustomerIdentityService,
    CustomerIdentityStore,
    SignedCustomerTokenVerifier,
    issue_customer_token,
)


UTC = timezone.utc
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SECRET = "customer-auth-test-secret-012345678901234567890123456"
BOT_TOKEN = "123456:telegram-test-token"


def _telegram_login(user_id: int, *, now: datetime = NOW) -> dict[str, str]:
    values = {"auth_date": str(int(now.timestamp())), "first_name": "Test", "id": str(user_id)}
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    values["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return values


class CustomerIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CustomerIdentityStore(Path(self.temp.name) / "identities.sqlite3")
        self.claims: dict[str, dict[str, str]] = {}
        self.service = CustomerIdentityService(
            SECRET,
            self.store,
            telegram_bot_token=BOT_TOKEN,
            telegram_bot_username="aurix_games_bot",
            google_client_id="games-client-id.apps.googleusercontent.com",
            google_token_verifier=lambda token, client_id: self.claims[token],
            cookie_secure=False,
        )

    def _google_login(self, subject: str, *, nonce: str | None = None) -> tuple[str, str]:
        challenge = nonce or self.service.issue_google_challenge(now=NOW)
        token = f"signed-google-token-{subject}-{len(self.claims)}"
        self.claims[token] = {
            "iss": "https://accounts.google.com",
            "nonce": challenge,
            "sub": subject,
        }
        return token, challenge

    def test_sessions_are_independent_revocable_and_survive_restart(self):
        first, owner = self.service.login_google(*self._google_login("same-player"), now=NOW)
        second, same_owner = self.service.login_google(*self._google_login("same-player"), now=NOW)
        self.assertEqual(owner.customer_id, same_owner.customer_id)
        self.assertNotEqual(first, second)
        self.service.logout(cookie_header=f"aurix_games_session={first}", now=NOW)
        restarted = SignedCustomerTokenVerifier(
            SECRET, session_store=CustomerIdentityStore(self.store.database)
        )
        with self.assertRaises(CustomerAuthError):
            restarted.verify(f"Bearer {first}", now=NOW)
        self.assertEqual(restarted.verify(f"Bearer {second}", now=NOW).customer_id, owner.customer_id)
        self.service.logout(f"Bearer {first}", now=NOW)
        self.service.logout(f"Bearer {second}", now=NOW)
        with self.assertRaises(CustomerAuthError):
            restarted.verify(cookie_header=f"aurix_games_session={second}", now=NOW)
        raw = self.store.database.read_bytes()
        self.assertNotIn(first.encode(), raw)
        self.assertNotIn(owner.session_id.encode(), raw)

    def test_registered_session_is_bound_to_customer_issuer_expiry_and_expires(self):
        token, owner = self.service.login_google(*self._google_login("bound-player"), now=NOW)
        for customer, expiry in [
            ("customer_other", owner.expires_at),
            (owner.customer_id, owner.expires_at + timedelta(seconds=1)),
        ]:
            forged = issue_customer_token(customer, SECRET, expires_at=expiry, session_id=owner.session_id)
            with self.assertRaises(CustomerAuthError):
                self.service.verifier.verify(f"Bearer {forged}", now=NOW)
        with self.assertRaises(CustomerAuthError):
            self.service.verifier.verify(f"Bearer {token}", now=owner.expires_at)
        legacy = issue_customer_token(owner.customer_id, SECRET, expires_at=owner.expires_at)
        with self.assertRaises(CustomerAuthError):
            self.service.verifier.verify(f"Bearer {legacy}", now=NOW)

    def test_telegram_login_is_verified_one_time_and_session_cookie_verifiable(self):
        payload = _telegram_login(1234)
        token, principal = self.service.login_telegram(payload, now=NOW)
        self.assertTrue(token)
        self.assertTrue(principal.customer_id.startswith("customer_"))
        self.assertEqual(
            SignedCustomerTokenVerifier(SECRET).verify(
                cookie_header=f"aurix_games_session={token}", now=NOW
            ).customer_id,
            principal.customer_id,
        )
        self.assertEqual(self.store.providers_for(principal.customer_id), ("telegram",))
        with self.assertRaises(CustomerAuthError):
            self.service.login_telegram(payload, now=NOW)

    def test_google_challenge_is_consumed_once_and_subject_is_account_key(self):
        token, nonce = self._google_login("google-subject-1")
        issued, principal = self.service.login_google(token, nonce, now=NOW)
        self.assertTrue(issued)
        self.assertEqual(self.store.providers_for(principal.customer_id), ("google",))
        with self.assertRaises(CustomerAuthError):
            self.service.login_google(token, nonce, now=NOW)

    def test_google_requires_matching_server_nonce_and_valid_issuer(self):
        nonce = self.service.issue_google_challenge(now=NOW)
        wrong_nonce_token, _ = self._google_login("google-subject-2", nonce="attacker-nonce")
        with self.assertRaises(CustomerAuthError):
            self.service.login_google(wrong_nonce_token, nonce, now=NOW)

        bad_issuer_token, bad_nonce = self._google_login("google-subject-3")
        self.claims[bad_issuer_token]["iss"] = "https://attacker.example"
        with self.assertRaises(CustomerAuthError):
            self.service.login_google(bad_issuer_token, bad_nonce, now=NOW)

    def test_google_backup_client_is_accepted_without_becoming_the_public_primary(self):
        calls = []

        def verify(token, client_id):
            calls.append(client_id)
            if client_id != "backup-client-id.apps.googleusercontent.com":
                raise ValueError("audience mismatch")
            return self.claims[token]

        backup_service = CustomerIdentityService(
            SECRET,
            self.store,
            google_client_id="primary-client-id.apps.googleusercontent.com",
            google_backup_client_id="backup-client-id.apps.googleusercontent.com",
            google_token_verifier=verify,
        )
        token, nonce = self._google_login("backup-account")
        issued, principal = backup_service.login_google(token, nonce, now=NOW)
        self.assertTrue(issued)
        self.assertEqual(
            principal.customer_id,
            self.store.customer_for("google", "backup-account", now=NOW),
        )
        self.assertEqual(calls, [
            "primary-client-id.apps.googleusercontent.com",
            "backup-client-id.apps.googleusercontent.com",
        ])
        self.assertEqual(
            backup_service.config_payload()["providers"]["google"]["client_id"],
            "primary-client-id.apps.googleusercontent.com",
        )

    def test_linking_requires_an_authenticated_account_and_never_merges_accounts(self):
        google_token, google_nonce = self._google_login("google-owner")
        _, google_principal = self.service.login_google(google_token, google_nonce, now=NOW)
        _, linked_principal = self.service.login_telegram(
            _telegram_login(55), link_customer_id=google_principal.customer_id, now=NOW
        )
        self.assertEqual(linked_principal.customer_id, google_principal.customer_id)
        self.assertEqual(self.store.providers_for(google_principal.customer_id), ("google", "telegram"))

        _, other_google = self.service.login_google(
            *self._google_login("google-already-owned"), now=NOW
        )
        with self.assertRaises(CustomerIdentityConflict):
            self.service.login_google(
                *self._google_login("google-already-owned"),
                link_customer_id=google_principal.customer_id,
                now=NOW,
            )
        self.assertNotEqual(other_google.customer_id, google_principal.customer_id)

    def test_public_config_exposes_only_public_provider_identifiers(self):
        config = self.service.config_payload()
        self.assertTrue(config["providers"]["telegram"]["enabled"])
        self.assertEqual(config["providers"]["telegram"]["bot_id"], "123456")
        self.assertTrue(config["providers"]["google"]["enabled"])
        self.assertEqual(config["providers"]["google"]["client_id"], "games-client-id.apps.googleusercontent.com")
        self.assertNotIn(BOT_TOKEN, json.dumps(config))
        self.assertNotIn(SECRET, json.dumps(config))

    def test_malformed_bot_token_is_never_exposed_as_a_public_bot_id(self):
        self.service.telegram_bot_token = "private-token-without-a-numeric-prefix"
        config = self.service.config_payload()
        self.assertFalse(config["providers"]["telegram"]["enabled"])
        self.assertIsNone(config["providers"]["telegram"]["bot_id"])
        self.assertNotIn(self.service.telegram_bot_token, json.dumps(config))

    def test_identity_database_is_private_and_rejects_symlinks(self):
        self.assertEqual(self.store.database.stat().st_mode & 0o777, 0o600)
        alias = Path(self.temp.name) / "alias.sqlite3"
        alias.symlink_to(self.store.database)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            CustomerIdentityStore(alias)


if __name__ == "__main__":
    unittest.main()
