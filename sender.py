"""
Sender — send WhatsApp messages via the Go bridge HTTP API.

The bridge exposes POST /api/send with JSON body:
  {"recipient": "<jid>", "message": "<text>"}
"""

import time
import requests
import config


def send_message(jid: str, text: str, retries: int = 3) -> bool:
    """
    Send a text message to a WhatsApp JID via the bridge.
    Returns True on success. Retries on connection errors.
    """
    url = f"{config.BRIDGE_URL}/api/send"
    for attempt in range(retries):
        try:
            resp = requests.post(
                url,
                json={"recipient": jid, "message": text},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("success", False)
            return False
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"[sender] Connection refused after {retries} retries")
                return False
        except Exception as e:
            print(f"[sender] Error: {e}")
            return False
    return False


def send_to_mechat(text: str) -> bool:
    """Send a message to the user's own MeChat (chat with themselves)."""
    from db import get_mechat_chat_jid
    return send_message(get_mechat_chat_jid(), text)
