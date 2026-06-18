from unittest.mock import MagicMock, patch
import pytest
from first_orion_client import FirstOrionClient


def _make_client(api_key="test-key", secret_key="test-secret"):
    return FirstOrionClient(api_key=api_key, secret_key=secret_key)


def test_get_token_returns_token_on_success():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"token": "tok-abc"}
    with patch("first_orion_client.requests.post", return_value=mock_resp):
        token = client._get_token()
    assert token == "tok-abc"


def test_get_token_raises_on_error():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"
    mock_resp.raise_for_status.side_effect = Exception("401")
    with patch("first_orion_client.requests.post", return_value=mock_resp):
        with pytest.raises(Exception):
            client._get_token()


def test_push_returns_true_on_200():
    client = _make_client()
    client._token = "tok-xyz"
    client._token_fetched_at = float("inf")  # prevents re-auth
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("first_orion_client.requests.post", return_value=mock_resp):
        result = client.push(a_number="+12125550199", b_number="+15551234567")
    assert result is True


def test_push_returns_false_on_4xx():
    client = _make_client()
    client._token = "tok-xyz"
    client._token_fetched_at = float("inf")
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    with patch("first_orion_client.requests.post", return_value=mock_resp):
        result = client.push(a_number="+12125550199", b_number="+15551234567")
    assert result is False


def test_push_refreshes_token_when_expired():
    client = _make_client()
    client._token = "old-token"
    client._token_fetched_at = 0  # expired

    auth_resp = MagicMock()
    auth_resp.status_code = 200
    auth_resp.json.return_value = {"token": "new-token"}

    push_resp = MagicMock()
    push_resp.status_code = 200

    with patch("first_orion_client.requests.post", side_effect=[auth_resp, push_resp]):
        result = client.push(a_number="+12125550199", b_number="+15551234567")
    assert result is True
    assert client._token == "new-token"


def test_build_from_secret_calls_secretsmanager():
    mock_sm = MagicMock()
    mock_sm.get_secret_value.return_value = {
        "SecretString": '{"api_key":"k1","secret_key":"s1"}'
    }
    with patch("first_orion_client.boto3.client", return_value=mock_sm):
        client = FirstOrionClient.build_from_secret("vip/firstorion/credentials")
    assert client._api_key == "k1"
    assert client._secret_key == "s1"
