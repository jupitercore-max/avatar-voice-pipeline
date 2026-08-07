"""Unified Ola Radio client composed from REST, PTT, voice, and persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from auth_session import AuthBundle
from config_store import ConfigStore
from persistence import ClientStore
from ptt_client import PttClient
from rest_client import EuRestApiClient, InternationalApiClient
from x_signer import DEFAULT_DEVICE_ID


def _items(response: dict, *keys: str) -> list:
    data = response.get("data", response)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


class OlaRadioClient:
    """High-level API. Network operations remain explicit."""

    def __init__(self, config_path: Optional[str] = None, state_path: Optional[str] = None):
        self.config_store = ConfigStore(config_path)
        self.config = self.config_store.load()
        jwt = self.config.get("international_api", {}).get("jwt", "")
        device_id = self.config.get("international_api", {}).get("headers", {}).get(
            "x-device-id", DEFAULT_DEVICE_ID
        )
        eu_token = self.config.get("auth", {}).get("token", "") or self.config.get(
            "rest_api_token", ""
        )
        eu_base = self.config.get("servers", {}).get("api", {}).get(
            "baseUrl", "https://euweb2c.olaradio.top:7002"
        )
        self.international = InternationalApiClient(jwt=jwt, device_id=device_id)
        self.eu = EuRestApiClient(token=eu_token, base_url=eu_base)
        default_state = Path.home() / ".local" / "share" / "ola-radio" / "client.sqlite3"
        self.store = ClientStore(state_path or str(default_state))
        self.ptt: Optional[PttClient] = None

    def close(self) -> None:
        if self.ptt:
            self.ptt.disconnect()
        self.store.close()

    def install_auth(self, bundle: AuthBundle, persist: bool = True) -> None:
        self.config = bundle.apply_to_config(self.config)
        self.international.jwt = bundle.international_jwt or self.international.jwt
        self.eu.token = bundle.eu_access_token or self.eu.token
        if bundle.device_id:
            self.international.device_id = bundle.device_id
        if persist:
            self.config_store.save(self.config)

    @property
    def user_id(self) -> int:
        return int(self.config.get("currentUser", {}).get("userId", 0))

    def sync_groups(self) -> list[dict]:
        response = self.eu.list_groups(self.user_id)
        groups = _items(response, "groups", "list", "records")
        self.store.upsert_groups(groups)
        return groups

    def create_group(self, group_name: str) -> dict:
        """Create a group through the EU REST surface."""
        return self.eu.create_group(group_name)

    def delete_group(self, group_id: int) -> dict:
        """Delete a group through the EU REST surface."""
        return self.eu.delete_group(group_id)

    def join_group(self, group_id: int) -> dict:
        """Join a group by numeric ID."""
        return self.eu.join_group(group_id)

    def join_group_with_code(self, invite_code: str) -> dict:
        """Join a group using its invitation code."""
        return self.eu.join_group_with_code(invite_code)

    def list_group_members(self, group_id: int) -> dict:
        """Fetch the group's member list."""
        return self.eu.list_group_members(group_id)

    def change_group_members(
        self,
        group_id: int,
        members: list,
        operation: str,
    ) -> dict:
        """Apply an official-client-style member mutation payload."""
        return self.eu.change_group_members(group_id, members, operation)

    def rename_group(self, group_id: int, group_name: str) -> dict:
        """Rename a group."""
        return self.eu.rename_group(group_id, group_name)

    def sync_contacts(self) -> list[dict]:
        response = self.international.get_friend_list()
        contacts = _items(response, "friends", "contacts", "list", "records")
        self.store.upsert_contacts(contacts)
        return contacts

    def sync_messages(self, group_id: int, page: int = 1, size: int = 50) -> list[dict]:
        response = self.eu.get_message_history(group_id, page=page, size=size)
        messages = _items(response, "messages", "list", "records")
        self.store.upsert_messages(messages)
        return messages

    def connect_ptt(self) -> dict:
        ptt_cfg = self.config.get("ptt", {})
        user = self.config.get("currentUser", {})
        server = self.config.get("servers", {}).get("ptt", {})
        self.ptt = PttClient(
            host=server.get("voiceServer", "eu-access2c.olaradio.top"),
            port=int(server.get("voicePort", 23001)),
        )
        result = self.ptt.login(
            login_name=str(user.get("userId", user.get("account", ""))),
            ptt_token=str(ptt_cfg.get("token", "")),
            ptt_uid=str(ptt_cfg.get("uid", user.get("pttUid", ""))),
            platform=ptt_cfg.get("platform", "linux"),
            device_model=ptt_cfg.get("device_model", "CX300"),
        )
        if result.get("success"):
            self.ptt.nickname = str(user.get("nickName", ""))
            self.ptt.on_text_message = lambda msg: self.store.upsert_messages([msg])
        return result

    def send_text(self, group_id: int, text: str) -> dict:
        if not self.ptt or not self.ptt.authenticated:
            return {"success": False, "error": "PTT is not authenticated"}
        result = self.ptt.send_text(str(group_id), text)
        if result.get("success"):
            import time
            self.store.upsert_messages([{
                "group_id": group_id, "text": text, "timestamp": int(time.time() * 1000),
                "sender_id": str(self.user_id), "sender_name": self.ptt.nickname,
            }], direction="out")
        return result
