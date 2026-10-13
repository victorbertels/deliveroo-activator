import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

_token_cache = {
    "token": None,
    "expires_at": None,
}


def getToken():
    import json
    import requests

    if _token_cache["token"] and _token_cache["expires_at"]:
        if datetime.now() < _token_cache["expires_at"]:
            return _token_cache["token"]

    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("CLIENT_ID and CLIENT_SECRET must be set in the environment or .env file")

    response = requests.post(
        "https://api.deliverect.io/oauth/token",
        headers={"Content-Type": "application/json"},
        json={
            "client_id": client_id,
            "client_secret": client_secret,
            "audience": "https://api.deliverect.com",
            "grant_type": "token",
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    token = body["access_token"]
    expires_in = body.get("expires_in", 3600)
    _token_cache["token"] = token
    _token_cache["expires_at"] = datetime.now() + timedelta(seconds=expires_in - 300)
    return token


def getHeaders():
    return {"Authorization": f"Bearer {getToken()}"}
