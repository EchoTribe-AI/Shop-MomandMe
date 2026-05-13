"""Amazon Creators API client — primary hydrator for Amazon ASINs.

Implements:
  - Token fetch + in-process cache (v2.x Cognito form-encoded, v3.x LwA JSON)
  - GetItems batched up to 10 ASINs per request
  - Response normalization to the shape amazon_trend_products expects

The vended `detailPageURL` is preserved verbatim — Amazon's docs explicitly
warn that altering returned URL parameters can break affiliate attribution.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Any

import requests

LOG = logging.getLogger(__name__)

GETITEMS_URL = "https://creatorsapi.amazon/catalog/v1/getItems"
DEFAULT_MARKETPLACE = "www.amazon.com"
DEFAULT_VERSION = "3.1"
MAX_BATCH = 10
REQUEST_TIMEOUT = 30
TOKEN_REFRESH_SKEW = 60  # refresh this many seconds before expiry

# Rate-limit guardrails for new-account TPS/TPD floor.
# Amazon's documented new-account allocation is 1 TPS / 8640 TPD. We pace at
# slightly above 1s between calls and cap total per-run batches well under
# the daily floor. Tunable via env without code changes.
MIN_REQUEST_INTERVAL_SEC = float(
    os.environ.get("AMAZON_CREATORS_MIN_INTERVAL_SEC", "1.05")
)
MAX_BATCHES_PER_RUN = int(
    os.environ.get("AMAZON_CREATORS_MAX_BATCHES_PER_RUN", "200")
)
MAX_RETRIES_ON_THROTTLE = int(
    os.environ.get("AMAZON_CREATORS_MAX_THROTTLE_RETRIES", "3")
)
# Backoff schedule (seconds) when the response body omits retryAfterSeconds.
_DEFAULT_BACKOFF = (2.0, 4.0, 8.0)


# Non-retryable client-side error reasons. If Amazon returns one of these, the
# request will never succeed without a config change — fail loud, don't retry.
_NON_RETRYABLE_REASONS = frozenset(
    {
        "InvalidPartnerTag",
        "InvalidAssociate",
        "FieldValidationFailed",
        "UnknownOperation",
        "CannotParse",
        "AssociateNotEligible",
        "AuthorizationFailed",
        "InvalidToken",
        "InvalidIssuer",
        "MissingClaim",
        "MissingKeyId",
        "UnsupportedClient",
        "InvalidClient",
        "MissingCredential",
    }
)

# Cognito (v2.x) token endpoints, keyed by leading version digit-pair.
_V2_TOKEN_ENDPOINTS = {
    "2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",
    "2.2": "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",
    "2.3": "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token",
}

# Login-with-Amazon (v3.x) token endpoints.
_V3_TOKEN_ENDPOINTS = {
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}


class AmazonCreatorsConfigError(RuntimeError):
    """Raised when required credentials/config are missing or invalid."""


class AmazonCreatorsAPIError(RuntimeError):
    """Raised on transient/retryable API failures."""


class AmazonCreatorsFatalError(AmazonCreatorsAPIError):
    """Raised on non-retryable client-side errors (bad tag, ineligible, etc).

    When this fires, retrying with the same input/config will keep failing.
    Callers should stop the run and surface the reason to the operator.
    """

    def __init__(self, reason: str, message: str, http_status: int = 0):
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message
        self.http_status = http_status


def _parse_error_body(text: str) -> tuple[str, str, str, int | None]:
    """Return (type, reason, message, retry_after_seconds) from an error body.

    Returns empty strings / None if the body is not parseable. Safe for any
    HTTP status code.
    """
    try:
        payload = json.loads(text) if text else {}
    except (ValueError, TypeError):
        return "", "", text[:300], None
    if not isinstance(payload, dict):
        return "", "", text[:300], None
    err_type = str(payload.get("type") or "")
    reason = str(payload.get("reason") or "")
    message = str(payload.get("message") or payload.get("error_description") or "")
    retry_after = payload.get("retryAfterSeconds")
    try:
        retry_after_i = int(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after_i = None
    return err_type, reason, message, retry_after_i


def detect_credential_family(version: str) -> str:
    """Return 'v2.x' or 'v3.x' for a credential version like '2.1' or '3.1'."""
    v = (version or "").strip()
    if v.startswith("2."):
        return "v2.x"
    if v.startswith("3."):
        return "v3.x"
    raise AmazonCreatorsConfigError(
        f"Unsupported AMAZON_CREATORS_CREDENTIAL_VERSION={version!r}; "
        "expected 2.1/2.2/2.3 or 3.1/3.2/3.3"
    )


def load_config() -> dict[str, str]:
    """Resolve config from environment, supporting both naming conventions.

    Preferred:  AMAZON_CREATORS_CLIENT_ID / AMAZON_CREATORS_CLIENT_SECRET
    Fallback:   CREDENTIAL_ID            / CREDENTIAL_SECRET   (Replit secret names)
    """
    client_id = (
        os.environ.get("AMAZON_CREATORS_CLIENT_ID")
        or os.environ.get("CREDENTIAL_ID")
        or ""
    ).strip()
    client_secret = (
        os.environ.get("AMAZON_CREATORS_CLIENT_SECRET")
        or os.environ.get("CREDENTIAL_SECRET")
        or ""
    ).strip()
    version = (
        os.environ.get("AMAZON_CREATORS_CREDENTIAL_VERSION") or DEFAULT_VERSION
    ).strip()
    partner_tag = (
        os.environ.get("AMAZON_PARTNER_TAG")
        or os.environ.get("AMAZON_AFFILIATE_TAG")
        or ""
    ).strip()
    marketplace = (
        os.environ.get("AMAZON_MARKETPLACE") or DEFAULT_MARKETPLACE
    ).strip()
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "version": version,
        "partner_tag": partner_tag,
        "marketplace": marketplace,
    }


class AmazonCreatorsAPI:
    """Thread-safe Creators API client with cached access tokens."""

    def __init__(self, config: dict[str, str] | None = None):
        cfg = config or load_config()
        self.client_id = cfg["client_id"]
        self.client_secret = cfg["client_secret"]
        self.version = cfg["version"]
        self.partner_tag = cfg["partner_tag"]
        self.marketplace = cfg["marketplace"]
        self.family = detect_credential_family(self.version) if self.version else ""
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()
        # Throttle pacing — tracks last successful or attempted call time so
        # we never burst above the new-account 1 TPS floor.
        self._last_request_at: float = 0.0
        self._pace_lock = threading.Lock()

    # ----- configuration / readiness -----

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.partner_tag and self.family)

    def missing_config(self) -> list[str]:
        missing = []
        if not self.client_id:
            missing.append("AMAZON_CREATORS_CLIENT_ID (or CREDENTIAL_ID)")
        if not self.client_secret:
            missing.append("AMAZON_CREATORS_CLIENT_SECRET (or CREDENTIAL_SECRET)")
        if not self.partner_tag:
            missing.append("AMAZON_PARTNER_TAG (or AMAZON_AFFILIATE_TAG)")
        if not self.version:
            missing.append("AMAZON_CREATORS_CREDENTIAL_VERSION")
        return missing

    # ----- token management -----

    def _token_endpoint(self) -> str:
        if self.family == "v2.x":
            ep = _V2_TOKEN_ENDPOINTS.get(self.version)
        else:
            ep = _V3_TOKEN_ENDPOINTS.get(self.version)
        if not ep:
            raise AmazonCreatorsConfigError(
                f"No token endpoint mapping for credential version {self.version!r}"
            )
        return ep

    def _fetch_token(self) -> tuple[str, int]:
        endpoint = self._token_endpoint()
        if self.family == "v2.x":
            basic = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode("utf-8")
            ).decode("ascii")
            resp = requests.post(
                endpoint,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": f"Basic {basic}",
                },
                data="grant_type=client_credentials&scope=creatorsapi/default",
                timeout=REQUEST_TIMEOUT,
            )
        else:  # v3.x — LwA. Scope uses `::` separator (distinct from v2.x's `/`).
            resp = requests.post(
                endpoint,
                headers={"Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": "creatorsapi::default",
                    }
                ),
                timeout=REQUEST_TIMEOUT,
            )
        if resp.status_code != 200:
            raise AmazonCreatorsAPIError(
                f"Token fetch failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
        payload = resp.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in") or 3600)
        if not token:
            raise AmazonCreatorsAPIError(
                f"Token response missing access_token: {payload!r}"
            )
        return token, expires_in

    def access_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and now < self._token_expires_at - TOKEN_REFRESH_SKEW:
                return self._token
            token, expires_in = self._fetch_token()
            self._token = token
            self._token_expires_at = now + expires_in
            LOG.info(
                "[CREATORS_API] Acquired access token (family=%s, expires_in=%ss)",
                self.family,
                expires_in,
            )
            return token

    # ----- GetItems -----

    DEFAULT_RESOURCES: tuple[str, ...] = (
        "images.primary.large",
        "images.primary.medium",
        "itemInfo.title",
        "itemInfo.byLineInfo",
        "itemInfo.classifications",
        "offersV2.listings.price",
        "offersV2.listings.availability",
        "parentASIN",
    )

    def get_items(
        self,
        asins: list[str],
        resources: tuple[str, ...] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Hydrate ASINs via GetItems with TPS pacing + throttle retry.

        - Batches are <=10 ASINs per request (one transaction each).
        - Calls are paced at >=MIN_REQUEST_INTERVAL_SEC apart (default 1.05s)
          to respect the new-account 1 TPS floor.
        - On 429 ThrottleException or 5xx, retries with the body-provided
          `retryAfterSeconds` (or exponential backoff if absent), up to
          MAX_RETRIES_ON_THROTTLE attempts per batch.
        - On non-retryable client errors (bad tag, ineligible, validation),
          raises AmazonCreatorsFatalError immediately so the caller stops
          burning the daily TPD budget on doomed retries.
        - Caps total batches per run at MAX_BATCHES_PER_RUN to protect TPD.

        Returns a dict keyed by ASIN. Missing ASINs are simply absent
        (404 ResourceNotFoundException is treated as "no data").
        """
        if not self.configured:
            raise AmazonCreatorsConfigError(
                f"Creators API not configured. Missing: {', '.join(self.missing_config())}"
            )
        unique = list(dict.fromkeys(a.strip() for a in asins if a and a.strip()))
        out: dict[str, dict[str, Any]] = {}
        batch_count = 0
        for start in range(0, len(unique), MAX_BATCH):
            if batch_count >= MAX_BATCHES_PER_RUN:
                LOG.warning(
                    "[CREATORS_API] Reached MAX_BATCHES_PER_RUN=%d; stopping to protect TPD budget. "
                    "Remaining ASINs will be picked up on the next enrich_pending() run.",
                    MAX_BATCHES_PER_RUN,
                )
                break
            batch = unique[start : start + MAX_BATCH]
            batch_count += 1
            try:
                items = self._get_items_batch_with_retry(
                    batch, resources or self.DEFAULT_RESOURCES
                )
            except AmazonCreatorsFatalError:
                # Non-retryable — bubble up so caller stops the whole run.
                raise
            except AmazonCreatorsAPIError as exc:
                LOG.warning(
                    "[CREATORS_API] Batch failed after retries (%d ASINs): %s",
                    len(batch), exc,
                )
                continue
            for raw in items:
                parsed = parse_item(raw)
                if parsed and parsed.get("asin"):
                    out[parsed["asin"]] = parsed
        return out

    # ----- pacing + retry -----

    def _wait_for_pacing(self) -> None:
        with self._pace_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < MIN_REQUEST_INTERVAL_SEC and self._last_request_at > 0:
                time.sleep(MIN_REQUEST_INTERVAL_SEC - elapsed)
            self._last_request_at = time.monotonic()

    def _get_items_batch_with_retry(
        self, batch: list[str], resources: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        last_exc: Exception | None = None
        token_refreshed_once = False
        for attempt in range(MAX_RETRIES_ON_THROTTLE + 1):
            self._wait_for_pacing()
            try:
                return self._get_items_batch(batch, resources)
            except _RetryableAuthError:
                # Token expired between cache check and call — refresh once.
                if token_refreshed_once:
                    raise
                token_refreshed_once = True
                with self._lock:
                    self._token = ""
                    self._token_expires_at = 0.0
                LOG.info("[CREATORS_API] Token rejected as expired; refreshing once and retrying")
                continue
            except _ThrottleRetryable as exc:
                last_exc = exc
                if attempt >= MAX_RETRIES_ON_THROTTLE:
                    break
                wait = exc.retry_after if exc.retry_after else _DEFAULT_BACKOFF[
                    min(attempt, len(_DEFAULT_BACKOFF) - 1)
                ]
                LOG.warning(
                    "[CREATORS_API] Throttled (attempt %d/%d): waiting %.1fs before retry",
                    attempt + 1, MAX_RETRIES_ON_THROTTLE + 1, wait,
                )
                time.sleep(wait)
                continue
        raise AmazonCreatorsAPIError(
            f"Throttled after {MAX_RETRIES_ON_THROTTLE + 1} attempts: {last_exc}"
        )

    def _get_items_batch(
        self, batch: list[str], resources: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        token = self.access_token()
        if self.family == "v2.x":
            auth = f"Bearer {token}, Version {self.version}"
        else:
            auth = f"Bearer {token}"
        headers = {
            "Authorization": auth,
            "Content-Type": "application/json",
            "x-marketplace": self.marketplace,
        }
        body = {
            "itemIds": batch,
            "itemIdType": "ASIN",
            "marketplace": self.marketplace,
            "partnerTag": self.partner_tag,
            "resources": list(resources),
        }
        resp = requests.post(
            GETITEMS_URL,
            headers=headers,
            data=json.dumps(body),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError as exc:
                raise AmazonCreatorsAPIError(f"GetItems response not JSON: {exc}") from exc
            items = (payload.get("itemsResult") or {}).get("items") or []
            return items if isinstance(items, list) else []

        # Non-200: parse structured error and route by type/reason.
        err_type, reason, message, retry_after = _parse_error_body(resp.text)
        status = resp.status_code

        # 404 ResourceNotFoundException — invalid ASIN(s). Not an error from
        # the caller's perspective; behave as "no data for those ASINs".
        if status == 404 and err_type == "ResourceNotFoundException":
            LOG.info(
                "[CREATORS_API] ResourceNotFoundException for batch (asins=%s): %s",
                batch, message,
            )
            return []

        # 401 TokenExpired — bubble a typed retryable so the outer loop refreshes once.
        if status == 401 and reason == "TokenExpired":
            raise _RetryableAuthError(message or "token expired")

        # Non-retryable client errors — stop the run immediately.
        if reason in _NON_RETRYABLE_REASONS:
            raise AmazonCreatorsFatalError(reason, message, status)

        # 429 throttle or 5xx — retryable.
        if status == 429 or status >= 500:
            raise _ThrottleRetryable(
                f"HTTP {status} {err_type or 'error'}: {message or resp.text[:200]}",
                retry_after=retry_after,
            )

        # Anything else — treat as a generic non-retryable failure so we don't
        # silently burn TPD on something Amazon explicitly rejected.
        raise AmazonCreatorsFatalError(
            reason or err_type or "UnknownError",
            message or resp.text[:300],
            status,
        )


class _ThrottleRetryable(AmazonCreatorsAPIError):
    def __init__(self, msg: str, retry_after: int | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class _RetryableAuthError(AmazonCreatorsAPIError):
    """Internal — signals a one-shot token refresh + retry."""


# ---------- response normalization ----------


def _display_value(node: Any) -> str:
    if isinstance(node, dict):
        v = node.get("displayValue")
        if isinstance(v, str):
            return v.strip()
    return ""


def _pick_image(images: Any) -> str:
    if not isinstance(images, dict):
        return ""
    primary = images.get("primary") or {}
    for key in ("large", "medium", "small", "hiRes"):
        node = primary.get(key)
        if isinstance(node, dict):
            url = node.get("url")
            if isinstance(url, str) and url:
                return url
    return ""


def _pick_offer(offers_v2: Any) -> tuple[float | None, str, str, str, str]:
    """Return (amount, display_amount, currency, availability_type, availability_message)."""
    if not isinstance(offers_v2, dict):
        return None, "", "", "", ""
    listings = offers_v2.get("listings") or []
    if not isinstance(listings, list) or not listings:
        return None, "", "", "", ""
    # Prefer buy-box winner, else first.
    chosen = next((l for l in listings if isinstance(l, dict) and l.get("isBuyBoxWinner")), None)
    if chosen is None:
        chosen = next((l for l in listings if isinstance(l, dict)), {}) or {}
    price = chosen.get("price") or {}
    money = price.get("money") or {} if isinstance(price, dict) else {}
    amount = money.get("amount")
    try:
        amount_f = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount_f = None
    display_amount = ""
    if isinstance(money.get("displayAmount"), str):
        display_amount = money["displayAmount"]
    elif amount_f is not None:
        display_amount = f"${amount_f:.2f}"
    currency = money.get("currency") or ""
    avail = chosen.get("availability") or {}
    avail_type = avail.get("type") or "" if isinstance(avail, dict) else ""
    avail_msg = avail.get("message") or "" if isinstance(avail, dict) else ""
    return amount_f, display_amount, currency, avail_type, avail_msg


def parse_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single GetItems response item to our storage schema."""
    if not isinstance(item, dict):
        return {}
    asin = item.get("asin") or ""
    if not asin:
        return {}
    item_info = item.get("itemInfo") or {}
    title = _display_value((item_info.get("title") if isinstance(item_info, dict) else None))
    byline = item_info.get("byLineInfo") if isinstance(item_info, dict) else None
    brand = _display_value((byline or {}).get("brand") if isinstance(byline, dict) else None)
    manufacturer = _display_value(
        (byline or {}).get("manufacturer") if isinstance(byline, dict) else None
    )
    classifications = item_info.get("classifications") if isinstance(item_info, dict) else None
    product_group = _display_value(
        (classifications or {}).get("productGroup") if isinstance(classifications, dict) else None
    )
    binding = _display_value(
        (classifications or {}).get("binding") if isinstance(classifications, dict) else None
    )
    image_url = _pick_image(item.get("images"))
    amount, display_amount, currency, avail_type, avail_msg = _pick_offer(item.get("offersV2"))
    return {
        "asin": asin,
        "product_title": title,
        "image_url": image_url,
        "brand": brand or manufacturer,
        "category": product_group or binding,
        "current_price": amount,
        "price_display": display_amount,
        "currency": currency,
        "availability_type": avail_type,
        "availability_message": avail_msg,
        "parent_asin": item.get("parentASIN") or "",
        "detail_page_url": item.get("detailPageURL") or "",
    }
