"""First Orion INFORM push client.

Auth: POST https://api.firstorion.com/v1/auth
  Headers: X-SERVICE=auth, X-API-KEY, X-SECRET-KEY
  Response: {"token": "<jwt>"}

Push: POST https://api.firstorion.com/exchange/v1/calls/push
  Headers: Authorization=<token>, Content-Type=application/json
  Body: {"aNumber": "+1...", "bNumber": "+1..."}
"""
from __future__ import annotations

import json
import logging
import time

import boto3
import requests

logger = logging.getLogger(__name__)

_AUTH_URL = "https://api.firstorion.com/v1/auth"
_PUSH_URL = "https://api.firstorion.com/exchange/v1/calls/push"
_TOKEN_TTL_SECONDS = 270  # refresh before 5-min expiry


class FirstOrionClient:
    def __init__(self, *, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._token: str | None = None
        self._token_fetched_at: float = 0.0

    @classmethod
    def build_from_secret(cls, secret_arn: str, region: str = "us-east-1") -> "FirstOrionClient":
        """Build client from AWS Secrets Manager secret.

        Args:
            secret_arn: ARN or name of the secret containing api_key and secret_key.
            region: AWS region where the secret is stored.
        """
        sm = boto3.client("secretsmanager", region_name=region)
        raw = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
        creds = json.loads(raw)
        return cls(api_key=creds["api_key"], secret_key=creds["secret_key"])

    def _get_token(self) -> str:
        resp = requests.post(
            _AUTH_URL,
            headers={
                "X-SERVICE": "auth",
                "X-API-KEY": self._api_key,
                "X-SECRET-KEY": self._secret_key,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["token"]

    def _ensure_token(self) -> None:
        if self._token is None or time.time() - self._token_fetched_at > _TOKEN_TTL_SECONDS:
            self._token = self._get_token()
            self._token_fetched_at = time.time()
            logger.info("First Orion token refreshed")

    def push(self, *, a_number: str, b_number: str) -> bool:
        """Fire a single pre-call push. Returns True on HTTP 200, False otherwise.

        HIPAA note: full phone numbers are never logged; only last-4 digits appear in logs.
        """
        b_last4 = b_number[-4:] if b_number else "????"
        try:
            self._ensure_token()
            resp = requests.post(
                _PUSH_URL,
                json={"aNumber": a_number, "bNumber": b_number},
                headers={
                    "Authorization": self._token,
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("First Orion push success b_last4=****%s", b_last4)
                return True
            logger.warning("First Orion push failed status=%d b_last4=****%s", resp.status_code, b_last4)
            return False
        # auth failures and push failures both return False — callers cannot distinguish them
        except Exception as exc:
            logger.error("First Orion push exception: %s b_last4=****%s", type(exc).__name__, b_last4)
            return False
