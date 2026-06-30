"""
Heuristic scam/spam scoring for Discord members.

This is deliberately transparent and tunable: every signal adds to a score and
appends a human-readable reason. A member's total score is compared against a
threshold to decide "suspect / not suspect". Nothing here takes an action — the
server layer decides whether to timeout/kick/ban based on the score.

Tune WEIGHTS to your server. Higher = more aggressive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# --- Pattern libraries -------------------------------------------------------

INVITE_RE = re.compile(
    r"(discord\.gg/|discord(?:app)?\.com/invite/|\.gg/)", re.IGNORECASE
)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MASS_MENTION_RE = re.compile(r"@everyone|@here", re.IGNORECASE)

# Phrases that overwhelmingly show up in Discord scams (crypto, nitro, airdrops,
# fake support, gift-card bait). Lowercase; matched as substrings.
SCAM_PHRASES = [
    "free nitro",
    "nitro free",
    "free discord nitro",
    "steam gift",
    "steam nitro",
    "airdrop",
    "claim your",
    "claim now",
    "first 100 users",
    "first 1000",
    "crypto giveaway",
    "double your",
    "guaranteed profit",
    "guaranteed returns",
    "investment opportunity",
    "dm me to earn",
    "telegram @",
    "whatsapp +",
    "send me your seed",
    "seed phrase",
    "wallet connect",
    "connect your wallet",
    "verify your wallet",
    "metamask",
    "elon",
    "presale",
    "100x",
    "pump",
    "exclusive deal",
    "limited offer",
    "click the link",
    "i made $",
]

# Words that, combined with a URL, strongly imply phishing.
PHISH_WITH_LINK = [
    "login",
    "verify",
    "gift",
    "free",
    "claim",
    "reward",
    "bonus",
    "wallet",
    "airdrop",
]

# --- Tunable weights ---------------------------------------------------------

WEIGHTS = {
    "account_age_lt_1d": 35,
    "account_age_lt_7d": 20,
    "account_age_lt_30d": 8,
    "joined_lt_10m": 15,
    "joined_lt_1h": 8,
    "default_avatar": 10,
    "no_roles": 5,
    "invite_link": 25,
    "external_link": 12,
    "scam_phrase": 30,
    "phish_link_combo": 30,
    "mass_mention": 18,
    "duplicate_blast": 28,  # same message posted in multiple channels
    "all_caps_spam": 6,
}

# Score at/above which a member is reported as a suspect by default.
DEFAULT_THRESHOLD = 45


@dataclass
class MemberSignals:
    """Everything the scorer needs about one member, gathered by the server."""

    user_id: int
    name: str
    account_age_days: float | None  # None if unknown
    joined_age_minutes: float | None
    has_default_avatar: bool
    role_count: int
    messages: list[str] = field(default_factory=list)
    # channel_id -> normalized message text, to detect cross-channel blasts
    messages_by_channel: dict[int, str] = field(default_factory=dict)


@dataclass
class ScamScore:
    user_id: int
    name: str
    score: int
    reasons: list[str]

    @property
    def is_suspect(self) -> bool:
        return self.score >= DEFAULT_THRESHOLD

    def to_dict(self) -> dict:
        return {
            "user_id": str(self.user_id),
            "name": self.name,
            "score": self.score,
            "reasons": self.reasons,
            "suspect": self.score >= DEFAULT_THRESHOLD,
        }


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def score_member(sig: MemberSignals, threshold: int = DEFAULT_THRESHOLD) -> ScamScore:
    """Return a ScamScore for a member given their signals."""
    score = 0
    reasons: list[str] = []

    def add(key: str, label: str):
        nonlocal score
        pts = WEIGHTS[key]
        score += pts
        reasons.append(f"{label} (+{pts})")

    # --- Account & membership age ---
    age = sig.account_age_days
    if age is not None:
        if age < 1:
            add("account_age_lt_1d", f"account < 1 day old ({age:.1f}d)")
        elif age < 7:
            add("account_age_lt_7d", f"account < 7 days old ({age:.1f}d)")
        elif age < 30:
            add("account_age_lt_30d", f"account < 30 days old ({age:.0f}d)")

    joined = sig.joined_age_minutes
    if joined is not None:
        if joined < 10:
            add("joined_lt_10m", f"joined < 10 min ago ({joined:.0f}m)")
        elif joined < 60:
            add("joined_lt_1h", f"joined < 1 hour ago ({joined:.0f}m)")

    # --- Profile ---
    if sig.has_default_avatar:
        add("default_avatar", "default avatar")
    if sig.role_count == 0:
        add("no_roles", "no roles")

    # --- Message content ---
    text_blob = " ".join(sig.messages)
    low = text_blob.lower()

    if INVITE_RE.search(text_blob):
        add("invite_link", "posted a Discord invite link")

    urls = URL_RE.findall(text_blob)
    if urls:
        add("external_link", f"posted external link(s) ({len(urls)})")
        if any(w in low for w in PHISH_WITH_LINK):
            add("phish_link_combo", "link + phishing keyword (login/verify/claim...)")

    matched_phrases = [p for p in SCAM_PHRASES if p in low]
    if matched_phrases:
        # Charge once, but note up to 3 matched phrases for transparency.
        sample = ", ".join(matched_phrases[:3])
        add("scam_phrase", f"scam phrasing: {sample}")

    if MASS_MENTION_RE.search(text_blob):
        add("mass_mention", "used @everyone/@here")

    # Cross-channel duplicate blast: same normalized text in >= 2 channels.
    norm_texts = list(sig.messages_by_channel.values())
    norm_set = {_normalize(t) for t in norm_texts if t.strip()}
    if norm_texts and len(norm_texts) >= 2 and len(norm_set) == 1:
        add("duplicate_blast", "same message blasted across multiple channels")

    # Shouty spam (mostly caps, with some length).
    letters = [c for c in text_blob if c.isalpha()]
    if len(letters) >= 12:
        caps_ratio = sum(c.isupper() for c in letters) / len(letters)
        if caps_ratio > 0.7:
            add("all_caps_spam", "mostly uppercase / shouting")

    return ScamScore(sig.user_id, sig.name, score, reasons)
