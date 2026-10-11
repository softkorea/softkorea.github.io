"""Pseudonym vault: token <-> original map, restorable and saved encrypted (scrypt + Fernet)."""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import unicodedata
from pathlib import Path

from . import entities as E

VAULT_VERSION = 1
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SALT_BYTES = 16

# Separators ignored when comparing numeric IDs ("010-1234-5678" == "01012345678").
_ID_SEPARATORS = re.compile(r"[\s\-‐‑–—.]+")
_HANGUL_NAME = re.compile(r"[가-힣]+(?:[ \t]+[가-힣]+)+")

# Links in an LLM answer: a value restored inside one leaves the computer without anyone
# reading it (markdown images and link previews fetch the URL by themselves, a mailto: link
# carries it in the message). See link_spans().
_LINK = re.compile(
    r"(?i)(?:\b(?:https?|ftps?|wss?|file|mailto|sms):|\bwww\.|(?<=[(=\"'])//)[^\s<>\"'`]*")


def link_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of URL-like parts of `text` (http(s)/ftp/ws/file/mailto/sms links,
    www.-addresses, //host links in markdown or HTML attributes), up to the next space."""
    return [m.span() for m in _LINK.finditer(text)]


def _inside(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s < end and start < e for s, e in spans)


class VaultError(Exception):
    """Vault file cannot be opened: wrong passphrase, corrupted or unsupported file, conflict."""


def normalize_value(entity: str, value: str) -> str:
    """Key used to decide whether two originals are "the same value" for pseudonyms.

    Emails are casefolded; IDs made of ASCII letters/digits ignore spaces, hyphens and dots
    (and letter case); Korean names ignore spaces ("김 민준" == "김민준"); everything else
    only collapses whitespace. Width and composition do not matter: full-width "０１０" equals
    "010" and decomposed (NFD) Hangul equals the composed form.
    """
    v = unicodedata.normalize("NFKC", value).strip()
    if entity == E.EMAIL or "@" in v:
        return v.casefold()
    compact = _ID_SEPARATORS.sub("", v)
    if compact.isascii() and compact.isalnum() and any(c.isdigit() for c in compact):
        return compact.upper()
    if entity == E.PERSON and _HANGUL_NAME.fullmatch(v):
        return "".join(v.split())
    return " ".join(v.split())


def _derive_fernet(passphrase: str, salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8"))))


class Vault:
    """Consistent pseudonym tokens ("[이름1]") and the map needed to restore them."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}               # token -> original (first seen)
        self._keys: dict[tuple[str, str], str] = {}    # (entity, normalized) -> token
        self._counters: dict[str, int] = {}            # entity -> last number issued
        self._restore_rx: re.Pattern | None = None

    def __len__(self) -> int:
        return len(self.tokens)

    def items(self) -> list[tuple[str, str, str]]:
        """(entity, original, token) for every pseudonym, in the order tokens were issued.

        `original` is the first spelling seen for the token. Keys merged from another vault
        whose token is unknown here are skipped.
        """
        entity_of: dict[str, str] = {}
        for (entity, _norm), token in self._keys.items():
            entity_of.setdefault(token, entity)
        return [(entity_of[t], o, t) for t, o in self.tokens.items() if t in entity_of]

    def token_for(self, entity: str, original: str, label: str) -> str:
        """Return the token for this value, issuing "[{label}{n}]" (n per entity) if new."""
        key = (entity, normalize_value(entity, original))
        token = self._keys.get(key)
        if token is not None:
            return token
        n = self._counters.get(entity, 0)
        while True:  # skip numbers taken by another entity sharing the same label
            n += 1
            token = f"[{label}{n}]"
            if token not in self.tokens:
                break
        self._counters[entity] = n
        self._keys[key] = token
        self.tokens[token] = original
        self._restore_rx = None
        return token

    def restore(self, text: str, *, links: bool = True) -> str:
        """Replace every known token in `text` with its original value (longest token first).

        ``links=False`` leaves tokens inside URLs and e-mail links (:func:`link_spans`) as they
        are, so a link chosen by an LLM answer (``![](https://x.example/?p=[이름1])``) cannot
        carry the real value off the computer; :meth:`tokens_in_links` counts them."""
        if not self.tokens:
            return text
        rx = self._token_rx()
        if links:
            return rx.sub(lambda m: self.tokens[m.group(0)], text)
        spans = link_spans(text)
        if not spans:
            return rx.sub(lambda m: self.tokens[m.group(0)], text)

        def sub(m: "re.Match[str]") -> str:
            if _inside(m.start(), m.end(), spans):
                return m.group(0)
            return self.tokens[m.group(0)]

        return rx.sub(sub, text)

    def tokens_in_links(self, text: str) -> int:
        """How many known tokens in `text` sit inside a URL or e-mail link."""
        if not self.tokens:
            return 0
        spans = link_spans(text)
        if not spans:
            return 0
        return sum(1 for m in self._token_rx().finditer(text) if _inside(m.start(), m.end(), spans))

    def _token_rx(self) -> "re.Pattern[str]":
        if self._restore_rx is None:
            alts = sorted(self.tokens, key=lambda t: (-len(t), t))
            self._restore_rx = re.compile("|".join(map(re.escape, alts)))
        return self._restore_rx

    def merge(self, other: "Vault") -> None:
        """Add `other`'s mappings. A token meaning different originals in each raises VaultError."""
        clashes = [t for t, o in other.tokens.items() if t in self.tokens and self.tokens[t] != o]
        if clashes:
            raise VaultError(f"cannot merge vaults: {len(clashes)} token(s) map to different values")
        for token, original in other.tokens.items():
            self.tokens.setdefault(token, original)
        for key, token in other._keys.items():
            self._keys.setdefault(key, token)
        for entity, n in other._counters.items():
            self._counters[entity] = max(n, self._counters.get(entity, 0))
        self._restore_rx = None

    # ------------------------------------------------------------------ persistence

    def _to_dict(self) -> dict:
        return {
            "tokens": self.tokens,
            "keys": [[ent, norm, tok] for (ent, norm), tok in self._keys.items()],
            "counters": self._counters,
        }

    @classmethod
    def _from_dict(cls, data: dict) -> "Vault":
        v = cls()
        v.tokens = {str(t): str(o) for t, o in data["tokens"].items()}
        v._keys = {(str(e), str(n)): str(t) for e, n, t in data["keys"]}
        v._counters = {str(e): int(n) for e, n in data["counters"].items()}
        return v

    def save(self, path: str | os.PathLike, passphrase: str) -> None:
        """Encrypt to `path` (JSON {"v","salt","data"}), written atomically with mode 0600."""
        if not passphrase:
            raise ValueError("vault passphrase must not be empty")
        salt = os.urandom(SALT_BYTES)
        plain = json.dumps(self._to_dict(), ensure_ascii=False).encode("utf-8")
        blob = {
            "v": VAULT_VERSION,
            "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
            "data": _derive_fernet(passphrase, salt).encrypt(plain).decode("ascii"),
        }
        path = Path(path)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(blob).encode("ascii"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: str | os.PathLike, passphrase: str) -> "Vault":
        """Decrypt a saved vault. Wrong passphrase or a damaged file raises VaultError."""
        from cryptography.fernet import InvalidToken

        raw = Path(path).read_bytes()
        try:
            blob = json.loads(raw.decode("utf-8"))
            version = blob["v"]
            salt = base64.urlsafe_b64decode(blob["salt"])
            data = blob["data"].encode("ascii")
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise VaultError("not a deid vault file") from e
        if version != VAULT_VERSION:
            raise VaultError(f"unsupported vault version {version!r}")
        try:
            plain = _derive_fernet(passphrase, salt).decrypt(data)
        except InvalidToken as e:
            raise VaultError("wrong passphrase or corrupted vault") from e
        try:
            return cls._from_dict(json.loads(plain.decode("utf-8")))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise VaultError("vault contents are malformed") from e
