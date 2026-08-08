"""One-click unsubscribe tokens.

**This must stay byte-compatible with `wizcodes_next/cloudflare-worker/inbound/
inbound-worker.js`.** The Worker verifies what this generates, and if the two
ever disagree every unsubscribe link in every sent email stops working at once —
silently, because a rejected token renders as a polite "link not recognised"
page. The recipient then presses "report spam" instead, which is the one metric
that ends a sending domain.

The shared format:

    <base64url(email)>.<base64url(hmac_sha256(email, UNSUBSCRIBE_SECRET))>

Signed rather than looked up, because the Worker's database role holds INSERT on
`core.inbound_events` and nothing else — it cannot SELECT a token table, and
granting it that read would let a leaked public credential dump every
contact-form submission and Wico conversation ever received.

## The headers matter more than the link

`List-Unsubscribe` and `List-Unsubscribe-Post` are invisible to the reader.
Gmail renders them as its own small "Unsubscribe" control next to the sender
name, which is a *trust* signal rather than a promotional one, and Outlook,
Yahoo and Apple Mail honour them too. They are not a Promotions risk; what sorts
mail into Promotions is HTML templates, images, tracking pixels and multiple
links.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from wizcore.db.identity import normalize_email


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_token(email: str, secret: str) -> str:
    """Sign a normalised address. '' when either input is missing.

    The address is normalised first so the token the recipient clicks resolves
    to the same string `core.suppressions` is keyed on — otherwise someone could
    unsubscribe `John.Doe+news@gmail.com` and keep receiving mail at
    `johndoe@gmail.com`, which is the same mailbox.
    """
    address = normalize_email(email)
    if not (address and secret):
        return ""
    signature = hmac.new(secret.encode(), address.encode(), hashlib.sha256).digest()
    return f"{_b64url(address.encode())}.{_b64url(signature)}"


def make_url(email: str, secret: str, base_url: str) -> str:
    token = make_token(email, secret)
    if not (token and base_url):
        return ""
    return f"{base_url.rstrip('/')}/{token}"


def headers(email: str, config) -> dict[str, str]:
    """The RFC 8058 header pair, plus a mailto: fallback.

    The mailto: variant is what covers clients that implement neither one-click
    nor a rendered control — which is the difference between "works in Gmail"
    and "works in whatever the recipient actually uses".
    """
    url = make_url(email, config.unsubscribe_secret, config.unsubscribe_base_url)
    if not url:
        return {}
    reply_to = config.reply_to or config.from_email
    return {
        "List-Unsubscribe": f"<{url}>, <mailto:{reply_to}?subject=unsubscribe>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def footer(config) -> str:
    """The plain-text sign-off. Deliberately one sentence, not a footer block.

    It satisfies CAN-SPAM's postal-address requirement, gives the pressure-release
    valve that keeps complaints down, reads like a human wrote it, and adds
    nothing a Promotions classifier keys on.
    """
    address = config.postal_address or "WizCodes, Ahmedabad, India"
    return f'Not the right person? Reply "no" and I\'ll take you off the list. - {address}'
