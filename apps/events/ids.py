import secrets

# base62: 0-9A-Za-z, 62 symbols. No separators/ambiguous-char exclusions —
# kept as the full alphabet for maximum entropy per character; the goal is a
# short, URL-friendly id, not human-transcription-friendliness.
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ID_LENGTH = 8


def generate_short_id() -> str:
    """Return an 8-character base62 id for use as ``Event.id``.

    Uses ``secrets.choice`` (CSPRNG) rather than the ``random`` module —
    this value is a public, guessable-if-weak identifier embedded in
    shareable URLs, so it needs cryptographically secure randomness even
    though it isn't a security credential itself.

    62**8 ≈ 2.18e14 possible values — collision probability at this app's
    scale is negligible. This function itself has no DB access and doesn't
    retry; the caller (``EventCreateSerializer.create``) retries a few times
    on the database's ``IntegrityError`` for the rare case this collides.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(_ID_LENGTH))
