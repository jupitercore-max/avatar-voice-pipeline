#!/usr/bin/env python3
"""
rest_client.py — HTTP API client for Ola Radio International REST API.

Integrates the cracked X-Signature into every request automatically.
Uses the international API (international.olaradio.top) with JWT Bearer auth.

All API calls automatically:
  - Compute the x-signature header from path + timestamp + device_id
  - Set x-timestamp to current server-aligned time
  - Include JWT Bearer authorization
  - Include device ID and app metadata headers
"""

import argparse
import base64 as b64
import copy
import json
import logging
import os
import time
from typing import Optional, Any, Tuple

import requests

from x_signer import generate_x, DEFAULT_DEVICE_ID

logger = logging.getLogger("ola_radio.rest")

# ─── Constants ───────────────────────────────────────────────────────────────

API_BASE = "https://international.olaradio.top/alervites_radio_en/api"
TIMEOUT = 15
MAX_RETRIES = 3
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

# Standard headers required on all API requests
BASE_HEADERS = {
    "user-agent": "002003001",
    "x-lang": "en",
    "x-appserv-id": "alervites_radio_en",
    "content-type": "application/json",
    "x-device-id": DEFAULT_DEVICE_ID,
}


class InternationalApiClient:
    """
    HTTP client for the Ola Radio International REST API.

    Automatically handles:
      - X-Signature generation (cracked formula from x_signer.py)
      - X-Timestamp header (server-aligned milliseconds)
      - JWT Bearer authorization
      - Retry with exponential backoff
      - Token refresh on 401/403

    Attributes:
        jwt: Current JWT Bearer token.
        device_id: Device UUID for x-device-id and x-signature.
        base_url: API base URL.
        session: requests.Session for connection pooling.
    """

    def __init__(
        self,
        jwt: str = "",
        device_id: str = DEFAULT_DEVICE_ID,
        base_url: str = API_BASE,
    ):
        """
        Initialize the API client.

        Args:
            jwt: JWT Bearer token (without "Bearer " prefix).
            device_id: Device UUID.
            base_url: API base URL.
        """
        self.jwt = jwt
        self.device_id = device_id
        self.base_url = base_url
        self.session = requests.Session()
        self._time_delta = 0  # server time - local time (ms)

    # ─── Time Sync ────────────────────────────────────────────────────────────

    def _server_millis(self) -> int:
        """
        Get current time in milliseconds, aligned to server time.

        The app tracks userTimeDelta (server_time - local_time) and uses
        server-aligned timestamps for x-timestamp and x-signature.

        Returns:
            Server-aligned Unix timestamp in milliseconds.
        """
        return int(time.time() * 1000) + self._time_delta

    def sync_time(self) -> dict:
        """
        Sync local time with server time.

        When the server returns code 10004 (time out of sync), it includes
        sysTime in the response. We use that to calculate the delta.

        Returns:
            Dict with sync result.
        """
        # A simple request to get server time
        result = self.post("app-version/get-last", {"appName": "walkie_talkie", "systemType": "1"})
        if result.get("code") == 10004:
            sys_time = result.get("data", {}).get("sysTime", 0)
            if sys_time:
                self._time_delta = sys_time - int(time.time() * 1000)
                logger.info("Time synced: delta = %d ms", self._time_delta)
                return {"success": True, "delta": self._time_delta}
        return {"success": False, "error": "Could not sync time"}

    # ─── Header Building ──────────────────────────────────────────────────────

    def _build_headers(self, path: str, extra: Optional[dict] = None) -> dict:
        """
        Build complete header dict including x-signature.

        Args:
            path: API path (after /api/, e.g., "app-version/get-last").
            extra: Additional headers to merge.

        Returns:
            Complete header dict.
        """
        ts = self._server_millis()
        x = generate_x(path, ts, self.device_id)

        headers = dict(BASE_HEADERS)
        headers["x-device-id"] = self.device_id
        headers["x-timestamp"] = str(ts)
        headers["x"] = x

        if self.jwt:
            headers["authorization"] = f"Bearer {self.jwt}"

        if extra:
            headers.update(extra)

        return headers

    # ─── Core HTTP Methods ────────────────────────────────────────────────────

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        """
        Make a GET request with automatic x-signature.

        Args:
            path: API path after /api/ (e.g., "user/get-info").
            params: Query parameters.

        Returns:
            Parsed JSON response dict.
        """
        url = f"{self.base_url}/{path}"
        headers = self._build_headers(path)

        resp = self._request_with_retry("GET", url, headers, params=params)
        return self._parse_response(resp, path)

    def post(self, path: str, data: Any = None) -> dict:
        """
        Make a POST request with automatic x-signature.

        Args:
            path: API path after /api/.
            data: JSON-serializable body.

        Returns:
            Parsed JSON response dict.
        """
        url = f"{self.base_url}/{path}"
        headers = self._build_headers(path)

        resp = self._request_with_retry("POST", url, headers, json_data=data)
        return self._parse_response(resp, path)

    def put(self, path: str, data: Any = None) -> dict:
        """Make a PUT request with automatic x-signature."""
        url = f"{self.base_url}/{path}"
        headers = self._build_headers(path)

        resp = self._request_with_retry("PUT", url, headers, json_data=data)
        return self._parse_response(resp, path)

    def delete(self, path: str, params: Optional[dict] = None) -> dict:
        """Make a DELETE request with automatic x-signature."""
        url = f"{self.base_url}/{path}"
        headers = self._build_headers(path)

        resp = self._request_with_retry("DELETE", url, headers, params=params)
        return self._parse_response(resp, path)

    # ─── Retry Logic ──────────────────────────────────────────────────────────

    def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: dict,
        params: Optional[dict] = None,
        json_data: Any = None,
    ) -> requests.Response:
        """
        Execute HTTP request with retry and exponential backoff.

        Retries on transient errors (429, 5xx) and network errors.
        On 10004 (time sync), updates time delta and retries.
        """
        backoff = 1.0
        last_resp = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if method == "GET":
                    resp = self.session.get(url, headers=headers, params=params, timeout=TIMEOUT)
                elif method == "POST":
                    resp = self.session.post(url, headers=headers, json=json_data, timeout=TIMEOUT)
                elif method == "PUT":
                    resp = self.session.put(url, headers=headers, json=json_data, timeout=TIMEOUT)
                elif method == "DELETE":
                    resp = self.session.delete(url, headers=headers, params=params, timeout=TIMEOUT)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                # Check for time sync issue
                try:
                    body = resp.json()
                    if body.get("code") == 10004:
                        sys_time = body.get("data", {}).get("sysTime", 0)
                        if sys_time and attempt == 1:
                            self._time_delta = sys_time - int(time.time() * 1000)
                            logger.info("Auto-synced time: delta=%d ms", self._time_delta)
                            # Rebuild headers with corrected time
                            path = url.replace(self.base_url + "/", "")
                            headers = self._build_headers(path)
                            continue
                except (ValueError, KeyError):
                    pass

                # Retry on transient HTTP errors
                if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
                    logger.warning(
                        "HTTP %d on attempt %d/%d, retrying in %.1fs",
                        resp.status_code, attempt, MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    # Rebuild headers with fresh timestamp + signature
                    path = url.replace(self.base_url + "/", "")
                    headers = self._build_headers(path)
                    continue

                return resp

            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "Network error on attempt %d/%d: %s — retrying",
                        attempt, MAX_RETRIES, e,
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    path = url.replace(self.base_url + "/", "")
                    headers = self._build_headers(path)
                else:
                    raise

        return last_resp or resp

    def _parse_response(self, resp: requests.Response, path: str) -> dict:
        """
        Parse HTTP response to dict, handling various formats.

        Args:
            resp: requests.Response object.
            path: API path (for error messages).

        Returns:
            Parsed response dict.
        """
        if resp.status_code != 200:
            return {
                "error": f"http_{resp.status_code}",
                "message": resp.text[:500],
            }

        try:
            data = resp.json()
        except ValueError:
            return {"error": "invalid_json", "raw": resp.text[:500]}

        # Check for API-level errors
        code = data.get("code")
        if code == 10005:
            # The server uses the same code for an invalid signature and an
            # invalid/expired auth session.  Do not misdiagnose an expired JWT
            # as a rotated signing secret; archived-signature replay can
            # distinguish those cases with a controlled time-sync response.
            logger.warning("API rejected %s (code 10005: auth or signature)", path)
        elif code == 10004:
            logger.warning("time sync issue for %s", path)

        return data

    # ─── High-Level API Methods ───────────────────────────────────────────────

    def login(self, account: str, password: str) -> dict:
        """
        Login with account credentials to obtain JWT.

        POST /api/login

        Args:
            account: Account name or email.
            password: Account password.

        Returns:
            Response dict with JWT token on success.
        """
        return self.post("login", {"account": account, "password": password})

    def get_user_info(self, user_id: int) -> dict:
        """GET /api/user/get-info"""
        return self.get("user/get-info", params={"cacheUser": user_id})

    def get_unread_count(self) -> dict:
        """POST /api/msg/get-unread-cnt"""
        return self.post("msg/get-unread-cnt", {"id": 1})

    def get_group_list(self, user_id: int) -> dict:
        """POST /api/schema/group/get-list"""
        # POST doesn't take params in our client; include as part of URL path
        return self.post("schema/group/get-list", {"pageSize": 100})

    def get_app_version(self) -> dict:
        """POST /api/app-version/get-last"""
        return self.post("app-version/get-last", {"appName": "walkie_talkie", "systemType": "1"})

    def get_risk_rules(self) -> dict:
        """POST /api/risk-ctrl-rule/get-list"""
        return self.post("risk-ctrl-rule/get-list", {"pageSize": 100})

    def get_ad_list(self) -> dict:
        """POST /api/ad/get-list"""
        return self.post("ad/get-list", {})

    def get_banner_list(self) -> dict:
        """POST /api/banner/get-list"""
        return self.post("banner/get-list", {})

    def get_information_list(self) -> dict:
        """POST /api/information/get-list"""
        return self.post("information/get-list", {})

    def get_product_list(self) -> dict:
        """POST /api/product/get-list"""
        return self.post("product/get-list", {"id": 0})

    def get_peripherals_features(self) -> dict:
        """POST /api/peripherals-feature/get-list"""
        return self.post("peripherals-feature/get-list", {"pageSize": 100})

    def get_org_list(self) -> dict:
        """POST /api/org/get-org-list"""
        return self.post("org/get-org-list", {})

    def submit_user_trace(self, data: dict) -> dict:
        """POST /api/user/trace"""
        return self.post("user/trace", data)

    def get_friend_list(self) -> dict:
        """GET /api/superPttFriendList (documented app endpoint)."""
        return self.get("superPttFriendList")

    def add_friend(self, user_id: int) -> dict:
        """POST /api/superPttAddFriend."""
        return self.post("superPttAddFriend", {"userId": int(user_id)})

    def get_super_ptt_group_list(self) -> dict:
        """GET /api/superPttGroupList."""
        return self.get("superPttGroupList")


# ─── EU REST API Client (separate auth system) ───────────────────────────────


class EuRestApiClient:
    """
    EU REST API client for Ola Radio (euweb2c.olaradio.top:7002).

    Uses pc-access-token header (not JWT). Does NOT use x-signature.
    Used for PTT-related endpoints: device info, group management,
    message history.
    """

    def __init__(self, token: str = "", base_url: str = "https://euweb2c.olaradio.top:7002"):
        self.token = token
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "user-agent": "002003001",
            "x-lang": "en",
            "x-appserv-id": "alervites_radio_en",
        })

    def _headers(self) -> dict:
        h = dict(self.session.headers)
        if self.token:
            h["pc-access-token"] = self.token
        return h

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=TIMEOUT)
        return self._parse(resp)

    def post(self, path: str, data: Any = None) -> dict:
        url = f"{self.base_url}{path}"
        resp = self.session.post(url, headers=self._headers(), json=data, timeout=TIMEOUT)
        return self._parse(resp)

    def _parse(self, resp: requests.Response) -> dict:
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}", "message": resp.text[:500]}
        try:
            return resp.json()
        except ValueError:
            return {"error": "invalid_json", "raw": resp.text[:500]}

    # ─── EU API Methods ───────────────────────────────────────────────────────

    def get_ptt_info(self, uid: int) -> dict:
        """GET /api/app/ptt/info"""
        return self.get("/api/app/ptt/info", params={"uid": uid})

    def list_devices(self) -> dict:
        """GET /api/app/ptt/list"""
        return self.get("/api/app/ptt/list")

    def list_groups(self, uid: int) -> dict:
        """GET /api/app/group/list"""
        return self.get("/api/app/group/list", params={"uid": uid})

    @staticmethod
    def _group_id(group_id: int) -> int:
        """Normalize a group ID without allowing empty/negative mutations."""
        value = int(group_id)
        if value <= 0:
            raise ValueError("group_id must be a positive integer")
        return value

    @staticmethod
    def _required_text(value: str, field: str) -> str:
        """Normalize a required API string."""
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{field} must not be empty")
        return normalized

    @staticmethod
    def _member_ids(members: list) -> list[int]:
        """Normalize and de-duplicate a non-empty member-ID list."""
        normalized = list(dict.fromkeys(int(member) for member in members))
        if not normalized or any(member <= 0 for member in normalized):
            raise ValueError("members must contain positive user IDs")
        return normalized

    # The paths and JSON key names below are present in the recovered official
    # client strings. Their live server semantics still require acceptance
    # testing with current credentials; keeping the wrappers here makes that
    # test explicit and prevents callers from assembling ad-hoc payloads.

    def create_group(self, group_name: str) -> dict:
        """POST candidate schema for /api/app/group/create."""
        return self.post(
            "/api/app/group/create",
            data={"groupName": self._required_text(group_name, "group_name")},
        )

    def delete_group(self, group_id: int) -> dict:
        """POST candidate schema for /api/app/group/delete."""
        return self.post(
            "/api/app/group/delete",
            data={"groupId": self._group_id(group_id)},
        )

    def join_group(self, group_id: int) -> dict:
        """POST candidate schema for /api/app/group/join."""
        return self.post(
            "/api/app/group/join",
            data={"groupId": self._group_id(group_id)},
        )

    def join_group_with_code(self, invite_code: str) -> dict:
        """POST candidate schema for /api/app/group/join/withcode."""
        return self.post(
            "/api/app/group/join/withcode",
            data={"inviteCode": self._required_text(invite_code, "invite_code")},
        )

    def get_group_code(self, group_id: int) -> dict:
        """GET candidate schema for /api/app/group/code."""
        return self.get(
            "/api/app/group/code",
            params={"groupId": self._group_id(group_id)},
        )

    def list_group_members(self, group_id: int) -> dict:
        """GET candidate schema for /api/app/group/members."""
        return self.get(
            "/api/app/group/members",
            params={"groupId": self._group_id(group_id)},
        )

    def change_group_members(
        self,
        group_id: int,
        members: list,
        operation: str,
    ) -> dict:
        """POST candidate schema for adding/removing group members."""
        return self.post(
            "/api/app/group/members/change",
            data={
                "groupId": self._group_id(group_id),
                "members": self._member_ids(members),
                "operation": self._required_text(operation, "operation"),
            },
        )

    def rename_group(self, group_id: int, group_name: str) -> dict:
        """POST candidate schema for /api/app/group/name/modify."""
        return self.post(
            "/api/app/group/name/modify",
            data={
                "groupId": self._group_id(group_id),
                "groupName": self._required_text(group_name, "group_name"),
            },
        )

    def leave_group(self, group_id: int) -> dict:
        """Refuse to guess: no REST leave path was found in recovered strings."""
        self._group_id(group_id)
        raise NotImplementedError(
            "No verified EU REST leave endpoint is known; "
            "use the native pttGroupExit path after its schema is recovered"
        )

    def get_message_sessions(self, group_ids: list) -> dict:
        """POST /api/app/message/group/session"""
        ts = int(time.time())
        body = [{"id": gid, "recvtype": "16", "timestamp": ts} for gid in group_ids]
        return self.post("/api/app/message/group/session", data=body)

    def get_message_history(self, group_id: int, page: int = 1, size: int = 20) -> dict:
        """POST /api/app/message/group/history"""
        return self.post(
            "/api/app/message/group/history",
            data={"id": group_id, "recvtype": "16", "page": page, "size": size},
        )

    def get_server_ip(self, domain: str = "eu-access2c.olaradio.top") -> dict:
        """GET /anon/server/sdk/ip"""
        return self.get("/anon/server/sdk/ip", params={"domain": domain})


# ─── Token Management ───────────────────────────────────────────────────────


# EU API refresh endpoints (in priority order)
EU_REFRESH_PATHS = ["/user/flush-token", "/auth/refreshToken", "/refresh-token"]

# International API refresh paths (in priority order)
INT_REFRESH_PATHS = ["auth/refreshToken", "user/flush-token", "refresh-token"]

# Path to config.json relative to this file
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class TokenManager:
    """
    Manages token lifecycle for both Ola Radio API systems.

    - International API: JWT Bearer (1-hour expiry, x-signature required)
    - EU REST API: pc-access-token header (unknown TTL, no x-signature)

    Refresh strategies (tried in order):
      1. International API refresh endpoints with current JWT
      2. EU API flush-token with current pc-access-token
      3. International API login with stored credentials (requires password)

    If all automated strategies fail, manual capture via capture_traffic.py
    is the fallback (documented in the status report).
    """

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self.config = self._load_config()

    # ─── Config ───────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        with open(self.config_path) as f:
            return json.load(f)

    def _save_config(self) -> None:
        """Persist config atomically with owner-only permissions."""
        from config_store import ConfigStore
        ConfigStore(self.config_path).save(self.config)
        logger.info("Config updated: %s", self.config_path)

    # ─── JWT Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _decode_jwt_payload(jwt: str) -> dict:
        """Decode the payload section of a JWT without verification."""
        parts = jwt.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        # Fix padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        try:
            decoded = b64.urlsafe_b64decode(payload_b64)
            return json.loads(decoded)
        except Exception:
            return {}

    def _get_jwt(self) -> str:
        return self.config.get("international_api", {}).get("jwt", "")

    def _get_eu_token(self) -> str:
        return (
            self.config.get("auth", {}).get("token", "")
            or self.config.get("rest_api_token", "")
            or self.config.get("headers", {}).get("pc-access-token", "")
        )

    def _get_device_id(self) -> str:
        return (
            self.config.get("international_api", {})
            .get("headers", {})
            .get("x-device-id", DEFAULT_DEVICE_ID)
        )

    def _get_user_id(self) -> int:
        return (
            self.config.get("currentUser", {}).get("userId", 0)
            or self.config.get("credentials", {}).get("userId", 0)
        )

    # ─── Status ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        """
        Check token status for both API systems.

        Returns dict with keys:
          - jwt_present, jwt_payload, jwt_expired, jwt_expires_in_secs
          - eu_token_present
          - live_jwt_check (from API call)
          - live_eu_check (from API call)
        """
        result = {}

        # ── JWT (International API) ──
        jwt = self._get_jwt()
        result["jwt_present"] = bool(jwt)
        if jwt:
            payload = self._decode_jwt_payload(jwt)
            result["jwt_payload"] = payload
            exp = payload.get("exp", 0)
            now = int(time.time())
            result["jwt_expired"] = now > exp
            result["jwt_expires_in_secs"] = exp - now if exp else None
            result["jwt_expires_human"] = self._secs_human(exp - now) if exp else "unknown"

        # ── EU pc-access-token ──
        eu_token = self._get_eu_token()
        result["eu_token_present"] = bool(eu_token)

        return result

    @staticmethod
    def _secs_human(secs: int) -> str:
        if secs < 0:
            return f"expired {-secs // 3600}h {(-secs) % 3600 // 60}m ago"
        return f"in {secs // 3600}h {secs % 3600 // 60}m"

    # ─── Live Token Checks ────────────────────────────────────────────────

    def check_jwt_live(self) -> dict:
        """
        Test the current JWT against the International API.
        Uses a lightweight endpoint (app-version/get-last).
        """
        jwt = self._get_jwt()
        if not jwt:
            return {"valid": False, "error": "no JWT in config"}

        client = InternationalApiClient(
            jwt=jwt, device_id=self._get_device_id()
        )
        try:
            resp = client.get_app_version()
            code = resp.get("code")
            if code == 0:
                return {"valid": True, "detail": "JWT accepted — API returned success"}
            elif code == 10005:
                return {"valid": False, "error": "JWT expired or x-signature rejected (code 10005)"}
            elif code == 10004:
                # Time sync issue — auto-sync and retry
                sys_time = resp.get("data", {}).get("sysTime", 0)
                if sys_time:
                    client._time_delta = sys_time - int(time.time() * 1000)
                    resp2 = client.get_app_version()
                    if resp2.get("code") == 0:
                        return {"valid": True, "detail": "JWT valid after time sync"}
                return {"valid": False, "error": f"time sync failed (code 10004)"}
            else:
                return {"valid": False, "error": f"unexpected code {code}: {resp.get('msg', '')}"}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    def check_eu_token_live(self) -> dict:
        """
        Test the current pc-access-token against the EU API.
        Uses a lightweight anonymous-adjacent endpoint.
        """
        token = self._get_eu_token()
        if not token:
            return {"valid": False, "error": "no pc-access-token in config"}

        client = EuRestApiClient(token=token)
        try:
            resp = client.get("/api/app/group/list", params={"uid": self._get_user_id()})
            code = resp.get("code", resp.get("status"))
            if code in (0, 200):
                return {"valid": True, "detail": "pc-access-token accepted"}
            elif code == 403:
                return {"valid": False, "error": "token expired (403)"}
            elif code == 1001:
                return {"valid": False, "error": "token header rejected (1001)"}
            else:
                return {"valid": False, "error": f"unexpected code {code}: {resp.get('message', resp.get('msg', ''))}"}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    # ─── JWT Refresh (International API) ──────────────────────────────────

    def refresh_jwt(self) -> dict:
        """
        Attempt to refresh the International API JWT.

        Strategy (in order):
          1. POST /api/auth/refreshToken with current JWT
          2. POST /api/user/flush-token with current JWT
          3. POST /api/refresh-token with current JWT

        Returns dict with:
          - success (bool)
          - method (str) — which strategy worked
          - jwt (str) — new JWT on success (redacted)
          - error (str) — on failure
          - attempts (list) — per-strategy results
        """
        jwt = self._get_jwt()
        device_id = self._get_device_id()
        attempts = []

        if not jwt:
            return {"success": False, "error": "no JWT in config", "attempts": []}

        client = InternationalApiClient(jwt=jwt, device_id=device_id)

        for path in INT_REFRESH_PATHS:
            attempt = {"method": f"POST /api/{path}"}
            try:
                resp = client.post(path, data={})
                code = resp.get("code")
                attempt["code"] = code
                attempt["msg"] = resp.get("msg", "")

                if code == 0:
                    new_jwt = self._extract_jwt(resp)
                    if new_jwt:
                        self._store_jwt(new_jwt)
                        self._save_config()
                        attempt["result"] = "success"
                        attempts.append(attempt)
                        return {
                            "success": True,
                            "method": f"POST /api/{path}",
                            "jwt": "***redacted***",
                            "attempts": attempts,
                        }
                    else:
                        attempt["result"] = "no token in response"
                elif code == 404:
                    attempt["result"] = "endpoint not found"
                elif code == 10005:
                    attempt["result"] = "JWT expired / illegal access"
                else:
                    attempt["result"] = f"code {code}"
            except Exception as e:
                attempt["result"] = f"error: {e}"
            attempts.append(attempt)

        return {
            "success": False,
            "error": "All JWT refresh endpoints failed",
            "attempts": attempts,
        }

    def login_international(self, account: str, password: str) -> dict:
        """
        Full login to International API to obtain a fresh JWT.

        This bypasses the need for a valid existing JWT — the x-signature
        provides request integrity, and account+password provide user auth.

        Returns dict with success/method/jwt or error.
        """
        device_id = self._get_device_id()
        client = InternationalApiClient(jwt="", device_id=device_id)

        try:
            resp = client.post(
                "login",
                data={"account": account, "password": password},
            )
            code = resp.get("code")
            if code == 0:
                new_jwt = self._extract_jwt(resp)
                if new_jwt:
                    self._store_jwt(new_jwt)
                    self._save_config()
                    return {
                        "success": True,
                        "method": "POST /api/login",
                        "jwt": "***redacted***",
                    }
                return {"success": False, "error": "login succeeded but no JWT in response", "raw_code": code}
            elif code == 10005:
                return {
                    "success": False,
                    "error": "login rejected (code 10005: auth or signature)",
                    "raw_code": code,
                }
            elif code == 10004:
                # Time sync — retry
                sys_time = resp.get("data", {}).get("sysTime", 0)
                if sys_time:
                    client._time_delta = sys_time - int(time.time() * 1000)
                    resp2 = client.post("login", data={"account": account, "password": password})
                    if resp2.get("code") == 0:
                        new_jwt = self._extract_jwt(resp2)
                        if new_jwt:
                            self._store_jwt(new_jwt)
                            self._save_config()
                            return {"success": True, "method": "POST /api/login (after time sync)", "jwt": "***redacted***"}
                    return {"success": False, "error": f"login failed after time sync: code {resp2.get('code')}", "raw_code": resp2.get("code")}
                return {"success": False, "error": "time sync needed but no sysTime", "raw_code": code}
            else:
                msg = resp.get("msg", resp.get("message", ""))
                return {"success": False, "error": f"login failed: code={code} msg={msg}", "raw_code": code}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─── EU Token Refresh ─────────────────────────────────────────────────

    def refresh_eu_token(self) -> dict:
        """
        Attempt to refresh the EU REST API pc-access-token.

        Tries /user/flush-token, /auth/refreshToken, /refresh-token.
        Requires the current token to still be valid.
        """
        token = self._get_eu_token()
        attempts = []

        if not token:
            return {"success": False, "error": "no pc-access-token in config", "attempts": []}

        eu_base = self.config.get(
            "servers", {}
        ).get("api", {}).get(
            "baseUrl", "https://euweb2c.olaradio.top:7002"
        )

        headers = {
            "pc-access-token": token,
            "user-agent": "002003001",
            "x-lang": "en",
            "x-appserv-id": "alervites_radio_en",
            "content-type": "application/json",
        }

        for path in EU_REFRESH_PATHS:
            attempt = {"method": f"POST {path}"}
            try:
                resp = requests.post(
                    f"{eu_base}{path}",
                    headers=headers,
                    json={},
                    timeout=TIMEOUT,
                )
                attempt["status_code"] = resp.status_code

                if resp.status_code == 200:
                    data = resp.json()
                    code = data.get("code", data.get("status", -1))
                    attempt["code"] = code

                    if code in (0, 200):
                        new_token = (
                            data.get("data", {}).get("token", "")
                            if isinstance(data.get("data"), dict)
                            else ""
                        ) or data.get("token", "")

                        if new_token and new_token != token:
                            self._store_eu_token(new_token)
                            self._save_config()
                            attempt["result"] = "success"
                            attempts.append(attempt)
                            return {
                                "success": True,
                                "method": f"POST {path}",
                                "token": "***redacted***",
                                "attempts": attempts,
                            }
                        else:
                            attempt["result"] = "token unchanged (still valid)"
                    elif code == 403:
                        attempt["result"] = "token expired"
                    elif code == 1001:
                        attempt["result"] = "bootstrap token missing"
                    else:
                        attempt["result"] = f"code {code}"
                else:
                    attempt["result"] = f"HTTP {resp.status_code}"
            except Exception as e:
                attempt["result"] = f"error: {e}"
            attempts.append(attempt)

        return {
            "success": False,
            "error": "All EU token refresh endpoints failed",
            "attempts": attempts,
        }

    # ─── High-Level: ensure_valid_jwt ─────────────────────────────────────

    def ensure_valid_jwt(self, account: str = "", password: str = "") -> dict:
        """
        Ensure we have a valid, non-expired JWT.

        Checks expiry first, then tries refresh strategies.
        If credentials are provided, tries login as last resort.

        Returns dict with success/detail.
        """
        status = self.status()

        if not status.get("jwt_present"):
            if account and password:
                return self.login_international(account, password)
            return {
                "success": False,
                "error": "no JWT in config and no credentials provided",
            }

        if not status.get("jwt_expired"):
            # JWT still valid — verify live
            live = self.check_jwt_live()
            if live["valid"]:
                return {"success": True, "detail": "JWT still valid", "action": "none"}
            # JWT claims to be valid but server rejects it
            logger.warning("JWT not expired but server rejected: %s", live.get("error"))

        # Try refresh endpoints first
        refresh_result = self.refresh_jwt()
        if refresh_result["success"]:
            return {"success": True, "detail": "JWT refreshed", "action": "refresh", "method": refresh_result["method"]}

        # Try login if credentials provided
        if account and password:
            login_result = self.login_international(account, password)
            if login_result["success"]:
                return {"success": True, "detail": "JWT obtained via login", "action": "login"}
            return login_result

        return {
            "success": False,
            "error": "JWT expired, refresh failed, no credentials for login",
            "refresh_attempts": refresh_result.get("attempts", []),
            "manual_step": (
                "Run capture_traffic.py to intercept fresh tokens from the app, "
                "then update config.json. See TODO.md for details."
            ),
        }

    # ─── Storage Helpers ──────────────────────────────────────────────────

    def _extract_jwt(self, resp: dict) -> str:
        """Extract JWT from various response formats."""
        data = resp.get("data", {})
        if isinstance(data, dict):
            for key in ("token", "jwt", "accessToken", "access_token", "tokenValue"):
                val = data.get(key, "")
                if val and val.startswith("eyJ"):
                    return str(val)
        # Check top-level
        for key in ("token", "jwt"):
            val = resp.get(key, "")
            if val and val.startswith("eyJ"):
                return str(val)
        return ""

    def _store_jwt(self, jwt: str) -> None:
        """Store JWT in config, updating payload info."""
        self.config.setdefault("international_api", {})["jwt"] = jwt

        payload = self._decode_jwt_payload(jwt)
        if payload:
            self.config["international_api"]["jwt_payload"] = payload
            exp = payload.get("exp")
            orig = payload.get("orig_iat")
            if orig and exp:
                self.config["international_api"]["jwt_lifetime_hours"] = round((exp - orig) / 3600, 2)

    def _store_eu_token(self, token: str) -> None:
        """Store EU pc-access-token in all config locations."""
        self.config.setdefault("auth", {})["token"] = token
        self.config["rest_api_token"] = token
        self.config.setdefault("headers", {})["pc-access-token"] = token


# ─── Probe / Discovery ───────────────────────────────────────────────────────


def probe_endpoints(jwt: str = "", device_id: str = DEFAULT_DEVICE_ID) -> None:
    """
    Probe various API endpoints and report results.

    Useful for testing that x-signature is working.
    """
    client = InternationalApiClient(jwt=jwt, device_id=device_id)

    tests = [
        ("App Version", client.get_app_version),
        ("Risk Rules", client.get_risk_rules),
        ("Unread Count", client.get_unread_count),
    ]

    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            result = fn()
            code = result.get("code", "?")
            msg = result.get("msg", result.get("message", ""))
            print(f"  code={code} msg={msg}")
            if code == 0:
                print(f"  ✅ SUCCESS")
                print(f"  data: {json.dumps(result.get('data', {}), indent=2)[:200]}")
            elif code == 10005:
                print(f"  ❌ X-SIGNATURE REJECTED")
            elif code == 10004:
                print(f"  ⏰ TIME SYNC NEEDED")
                sys_time = result.get("data", {}).get("sysTime", 0)
                if sys_time:
                    print(f"     Server: {sys_time}, Local: {int(time.time()*1000)}")
            else:
                print(f"  ⚠️  code={code}")
        except Exception as e:
            print(f"  ERROR: {e}")


def cli_main():
    """CLI entry point for token management and API probing."""
    parser = argparse.ArgumentParser(
        description="Ola Radio REST client — token management and API tools",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # status
    sp_status = sub.add_parser("status", help="Show token status (no network calls)")
    sp_status.add_argument("--live", action="store_true", help="Also test tokens against live API")

    # refresh-jwt
    sp_rjwt = sub.add_parser("refresh-jwt", help="Refresh International API JWT")
    sp_rjwt.add_argument("--account", help="Account name/email for login fallback")
    sp_rjwt.add_argument("--password", help="Password (unsafe in process list; prefer prompt or OLA_PASSWORD)")

    # refresh-eu
    sp_reu = sub.add_parser("refresh-eu", help="Refresh EU API pc-access-token")

    # login
    sp_login = sub.add_parser("login", help="Login to International API for fresh JWT")
    sp_login.add_argument("--account", required=True, help="Account name/email")
    sp_login.add_argument("--password", help="Password (unsafe in process list; prefer prompt or OLA_PASSWORD)")

    # refresh-all
    sp_rall = sub.add_parser("refresh-all", help="Try all token refresh strategies and report")
    sp_rall.add_argument("--account", help="Account name/email for login fallback")
    sp_rall.add_argument("--password", help="Password (prefer prompt or OLA_PASSWORD)")

    # probe
    sp_probe = sub.add_parser("probe", help="Probe API endpoints with current tokens")

    # ensure
    sp_ensure = sub.add_parser("ensure", help="Ensure a valid JWT (check + refresh + login)")
    sp_ensure.add_argument("--account", help="Account for login fallback")
    sp_ensure.add_argument("--password", help="Password (prefer prompt or OLA_PASSWORD)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    tm = TokenManager()

    password = getattr(args, "password", "") or os.environ.get("OLA_PASSWORD", "")
    if not password and getattr(args, "account", ""):
        import getpass
        password = getpass.getpass("Ola Radio password: ")

    if args.command == "status":
        cmd_status(tm, live=getattr(args, "live", False))
    elif args.command == "refresh-jwt":
        cmd_refresh_jwt(tm, account=args.account or "", password=password)
    elif args.command == "refresh-eu":
        cmd_refresh_eu(tm)
    elif args.command == "login":
        cmd_login(tm, account=args.account, password=password)
    elif args.command == "probe":
        cmd_probe(tm)
    elif args.command == "refresh-all":
        cmd_refresh_all(tm, account=getattr(args, "account", "") or "", password=password)
    elif args.command == "ensure":
        cmd_ensure(tm, account=args.account or "", password=password)


def cmd_status(tm: TokenManager, live: bool = False):
    """Print token status."""
    s = tm.status()

    print("═" * 60)
    print("  Ola Radio Token Status")
    print("═" * 60)

    # JWT
    print("\n📡 International API (JWT):")
    if s.get("jwt_present"):
        payload = s.get("jwt_payload", {})
        expired = s.get("jwt_expired", True)
        human = s.get("jwt_expires_human", "unknown")
        print(f"  Present: ✅")
        print(f"  user_id:     {payload.get('user_id', '?')}")
        print(f"  token_ver:   {payload.get('token_version', '?')}")
        print(f"  issued:      {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(payload.get('orig_iat', 0)))}")
        print(f"  expires:     {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(payload.get('exp', 0)))}")
        print(f"  Status:      {'❌ EXPIRED ' + human if expired else '✅ Valid ' + human}")
    else:
        print("  Present: ❌ (no JWT configured)")

    # EU token
    print("\n📡 EU REST API (pc-access-token):")
    if s.get("eu_token_present"):
        print("  Present: ✅")
    else:
        print("  Present: ❌ (no token configured)")

    # Live checks
    if live:
        print("\n─" * 60)
        print("Live API checks...")

        print("\n  JWT check:")
        jwtr = tm.check_jwt_live()
        if jwtr["valid"]:
            print(f"    ✅ {jwtr['detail']}")
        else:
            print(f"    ❌ {jwtr['error']}")

        print("\n  EU token check:")
        eur = tm.check_eu_token_live()
        if eur["valid"]:
            print(f"    ✅ {eur['detail']}")
        else:
            print(f"    ❌ {eur['error']}")

    print("\n" + "═" * 60)


def cmd_refresh_jwt(tm: TokenManager, account: str = "", password: str = ""):
    """Attempt JWT refresh."""
    print("═" * 60)
    print("  Refreshing International API JWT")
    print("═" * 60)

    result = tm.refresh_jwt()

    if result["success"]:
        print(f"\n✅ JWT refreshed via {result['method']}")
        print(f"   Config updated: {tm.config_path}")
        return

    print(f"\n❌ JWT refresh failed: {result['error']}")
    for a in result.get("attempts", []):
        print(f"   {a['method']}: {a.get('result', 'unknown')} (code={a.get('code', 'n/a')})")

    if account and password:
        print("\nAttempting login fallback...")
        login_result = tm.login_international(account, password)
        if login_result["success"]:
            print(f"\n✅ JWT obtained via {login_result['method']}")
            print(f"   Config updated: {tm.config_path}")
            return
        print(f"\n❌ Login also failed: {login_result['error']}")
    else:
        print("\n   No credentials provided for login fallback.")

    print("\n📋 Manual step: Run capture_traffic.py to intercept fresh tokens.")


def cmd_refresh_eu(tm: TokenManager):
    """Attempt EU token refresh."""
    print("═" * 60)
    print("  Refreshing EU REST API pc-access-token")
    print("═" * 60)

    result = tm.refresh_eu_token()

    if result["success"]:
        print(f"\n✅ Token refreshed via {result['method']}")
        print(f"   Config updated: {tm.config_path}")
        return

    print(f"\n❌ EU token refresh failed: {result['error']}")
    for a in result.get("attempts", []):
        print(f"   {a['method']}: {a.get('result', 'unknown')} (code={a.get('code', 'n/a')})")

    print("\n📋 EU token cannot be refreshed automatically if expired.")
    print("   Run capture_traffic.py to intercept a fresh token from the app.")


def cmd_login(tm: TokenManager, account: str, password: str):
    """Login to get fresh JWT."""
    print("═" * 60)
    print("  International API Login")
    print("═" * 60)

    result = tm.login_international(account, password)

    if result["success"]:
        print(f"\n✅ Login successful via {result['method']}")
        print(f"   Config updated: {tm.config_path}")
        # Verify
        s = tm.status()
        if s.get("jwt_payload"):
            print(f"   New JWT expires: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(s['jwt_payload'].get('exp', 0)))}")
        return

    print(f"\n❌ Login failed: {result['error']}")
    if "raw_code" in result:
        print(f"   Server code: {result['raw_code']}")


def cmd_refresh_all(tm: TokenManager, account: str = "", password: str = ""):
    """Try all refresh strategies and give a comprehensive report."""
    print("═" * 60)
    print("  Full Token Refresh — All Strategies")
    print("═" * 60)

    jwt_ok = False
    eu_ok = False

    # 1. JWT refresh endpoints
    print("\n1\ufe0f\u20e3  International API — JWT refresh endpoints...")
    r = tm.refresh_jwt()
    if r["success"]:
        print(f"   \u2705 JWT refreshed via {r['method']}")
        jwt_ok = True
    else:
        for a in r.get("attempts", []):
            print(f"   \u274c {a['method']}: {a.get('result', 'unknown')}")

    # 2. EU token refresh endpoints
    if not jwt_ok:
        print("\n2\ufe0f\u20e3  EU API — pc-access-token refresh endpoints...")
        r = tm.refresh_eu_token()
        if r["success"]:
            print(f"   \u2705 EU token refreshed via {r['method']}")
            eu_ok = True
        else:
            for a in r.get("attempts", []):
                print(f"   \u274c {a['method']}: {a.get('result', 'unknown')}")

    # 3. Login if credentials provided
    if not jwt_ok and account and password:
        print("\n3\ufe0f\u20e3  International API — login with credentials...")
        r = tm.login_international(account, password)
        if r["success"]:
            print(f"   \u2705 JWT obtained via {r['method']}")
            jwt_ok = True
        else:
            print(f"   \u274c Login failed: {r['error']}")

    # Summary
    print("\n" + "═" * 60)
    print("  Summary")
    print("═" * 60)
    if jwt_ok:
        print("  \u2705 International API JWT: refreshed")
    else:
        print("  \u274c International API JWT: expired, no automated refresh available")
    if eu_ok:
        print("  \u2705 EU REST API token: refreshed")
    else:
        print("  \u274c EU REST API token: expired, no automated refresh available")

    if not jwt_ok and not eu_ok:
        print("\n\U0001f4cb Manual capture required:")
        print("   Both tokens are expired and cannot be refreshed via API.")
        print("   The login endpoints require an existing valid token (chicken-and-egg).")
        print("\n   To get fresh tokens:")
        print("   1. Run: sudo python3 archive/capture_traffic.py")
        print("   2. Restart the Ola Radio app")
        print("   3. Wait for capture to save tokens to /tmp/ola-fresh-*.txt")
        print("   4. Run: python3 rest_client.py ensure")
        print("      (after updating config.json with captured tokens)")
        print("\n   PTT voice is unaffected — it uses separate credentials.")


def cmd_probe(tm: TokenManager):
    """Probe API endpoints."""
    jwt = tm._get_jwt()
    device_id = tm._get_device_id()

    print(f"JWT: {jwt[:20]}..." if jwt else "JWT: (none)")
    print(f"Device: {device_id}")
    probe_endpoints(jwt=jwt, device_id=device_id)


def cmd_ensure(tm: TokenManager, account: str = "", password: str = ""):
    """Ensure valid JWT."""
    print("═" * 60)
    print("  Ensuring Valid JWT")
    print("═" * 60)

    result = tm.ensure_valid_jwt(account=account, password=password)

    if result["success"]:
        action = result.get("action", "?")
        if action == "none":
            print(f"\n✅ JWT is valid ({result['detail']})")
        else:
            print(f"\n✅ {result['detail']} via {result.get('method', action)}")
            print(f"   Config updated: {tm.config_path}")
        return

    print(f"\n❌ {result['error']}")
    if "refresh_attempts" in result:
        for a in result["refresh_attempts"]:
            print(f"   {a['method']}: {a.get('result', 'unknown')}")
    if "manual_step" in result:
        print(f"\n📋 {result['manual_step']}")


if __name__ == "__main__":
    cli_main()
