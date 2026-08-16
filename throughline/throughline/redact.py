"""Scrub server-local filesystem paths, internal network topology, and
decider identity out of every HTTP response body before it leaves the
process, for a caller who presented no valid caller token.

throughline's read surface is deliberately public (see ``PUBLIC_VERIFICATION``
in ``app.py``): anyone can read the ledger, the approval queue, the dataset
registry and the effect config to verify the chain for themselves. Public
does not mean "leaks the deploy host's filesystem" — a reviewer found
``/home/<user>/...`` paths and ``127.0.0.1:860x`` internal addresses coming
back on anonymous calls to ``/datasets`` and ``/ledger``. Both are baked into
the append-only ledger already (``config.loaded``, ``config.refused`` and
similar entries record the config file's fully resolved source path at the
moment it was loaded), so the fix cannot be "edit the ledger" — the chain is
append-only and rewriting it is the one unforgivable act for this product.

Instead every response is scrubbed at the serialization boundary, on the way
OUT, leaving the ledger file on disk untouched. A path becomes its basename —
a stable, useful identifier ("effects.yaml") with the server's directory
layout and OS username removed. A loopback or private address with a port —
the shape an internal peer's URL takes — becomes a fixed placeholder.

This runs for every response, not just the two endpoints a reviewer happened
to try, because the same ledger entries that leaked through ``/ledger`` reach
a caller through ``/ledger/verify``'s replay, a cause ``walk``, and anywhere
else a raw entry or a config/dataset object is echoed back.

A second, distinct finding (issue #9): ``GET /approvals`` (and every other
anonymous-reachable route that echoes a ledger entry or an approval) named
the DECIDER — ``decided_by``, mirrored as ``approved_by`` on the executed
effect — to a caller who presented nothing at all: an OIDC subject, a GitHub
handle, a bare email address. WHO decided is a different fact from WHETHER
the chain is intact; the hash chain and its verification stay public (that is
this product's differentiator), but the identity of the human behind a
decision is not required to check chain linkage and is not defensible to
publish to an anonymous stranger. ``pseudonymize_identity`` replaces a
decider value with a short, stable, non-reversible token derived from a
per-deployment secret (the substrate's caller token) rather than blanking it
outright: two ledger entries that share a decider still visibly share the
same pseudonym, which is what an auditor checking the chain for a single
rogue or a single legitimate approver repeated across entries actually
needs — they just never learn who that person is. An authenticated caller
(one presenting the caller token) is exempt, exactly like the path/topology
scrub above.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any, Optional

#: A POSIX absolute path of two or more segments, e.g. "/home/user/x/y" or
#: "/etc/foo". A single segment ("/ledger", "/config") is left alone — that
#: is an API route or a word, not a filesystem escape.
_ABS_PATH_RE = re.compile(r"/(?:[^\s\"'()\[\]{}:,]+/)+[^\s\"'()\[\]{}:,]+")

#: A loopback / private address paired with a port — the shape of an internal
#: peer's own listen address (siren, docket, breaker, blindspot, ...).
_INTERNAL_HOSTPORT_RE = re.compile(
    r"\b(?:127\.0\.0\.1|0\.0\.0\.0|localhost"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
    r")(?::\d{2,5})?\b"
)

#: What an internal host:port becomes on a public response.
INTERNAL_TOPOLOGY_PLACEHOLDER = "<internal>"

#: Field names that name the DECIDER — who approved or rejected, or who an
#: executed effect's approval is mirrored from. Not ``issuer`` (the
#: authority that vouched for the decider — organisational metadata an
#: auditor needs to check an attestation, not itself a personal identity)
#: and not ``produced_by`` (which rule/model produced a judgment — a system
#: component, not a human principal).
_IDENTITY_FIELD_NAMES = frozenset({"decided_by", "approved_by"})

#: A bare email address, wherever it shows up — a field we did not name
#: above, or embedded in free text (a refusal's ``reason``, quoting the
#: subject it refused). Defense in depth alongside the field-name scrub.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: What an identity value with no key available becomes. Should not occur in
#: practice (the middleware always has the substrate's caller token), but a
#: response must never contain a raw identity even if it did.
_IDENTITY_PLACEHOLDER = "<redacted-identity>"


def _scrub_path(match: "re.Match[str]") -> str:
    """A matched absolute path becomes its basename — a stable identifier,
    not a directory listing of the deploy host."""
    candidate = match.group(0)
    base = os.path.basename(candidate.rstrip("/"))
    return base or "<path>"


def scrub_text(value: str) -> str:
    """Redact absolute paths and internal host:port pairs out of one string."""
    value = _ABS_PATH_RE.sub(_scrub_path, value)
    value = _INTERNAL_HOSTPORT_RE.sub(INTERNAL_TOPOLOGY_PLACEHOLDER, value)
    return value


def pseudonymize_identity(value: str, key: bytes) -> str:
    """A decider value becomes a short, stable, non-reversible token.

    HMAC-SHA256 keyed on a per-deployment secret (the substrate's caller
    token — already secret, already minted per substrate, so this needs no
    new config surface), truncated to 12 hex characters. Stable: the same
    decider always hashes to the same token within one deployment, so an
    auditor can see two ledger entries share a decider without learning who
    it is. Non-reversible without the key: a caller cannot recover the
    identity by brute-forcing plausible names/emails against the public
    output, because the digest is keyed rather than a bare hash.
    """
    if not value:
        return value
    digest = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"anon-{digest[:12]}"


def _collect_identity_values(obj: Any, values: set[str]) -> None:
    """Find every string on record under a decider field name, anywhere in
    the payload, so the substring pass below can also catch that exact value
    if it is echoed again inside free text (e.g. a refusal's ``reason``)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _IDENTITY_FIELD_NAMES and isinstance(value, str) and value:
                values.add(value)
            _collect_identity_values(value, values)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _collect_identity_values(item, values)


def scrub_identity(obj: Any, key: bytes) -> Any:
    """Recursively replace decider identity with a stable pseudonym.

    Two passes over every string in the payload: known decider values
    (collected up front from ``decided_by``/``approved_by`` fields, wherever
    they occur) are substituted wherever they appear verbatim — including
    inside a refusal's prose ``reason``, which quotes the subject it
    refused — and then any bare email address that slipped in some other
    way is caught by the regex pass. Both use the same keyed hash, so a
    value pseudonymized by either pass reads identically.
    """
    values: set[str] = set()
    _collect_identity_values(obj, values)
    # Longest first: a shorter identity value must never partially consume a
    # longer one that contains it.
    ordered = sorted(values, key=len, reverse=True)

    def _replace_text(text: str) -> str:
        for original in ordered:
            if original and original in text:
                text = text.replace(original, pseudonymize_identity(original, key))
        return _EMAIL_RE.sub(lambda m: pseudonymize_identity(m.group(0), key), text)

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return _replace_text(value)
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        if isinstance(value, tuple):
            return tuple(_walk(v) for v in value)
        return value

    return _walk(obj)


def scrub(obj: Any, *, identity_key: Optional[bytes] = None) -> Any:
    """Recursively scrub a JSON-shaped value (dict / list / str / scalar).

    Never mutates the ledger or the config on disk — this runs only on the
    value about to be serialized into an HTTP response.

    ``identity_key`` is the substrate's caller-token secret, used to
    pseudonymize decider identity (see ``scrub_identity``). Omitted, decider
    values still cannot leak raw — they fall back to a fixed placeholder
    rather than being passed through, since no response should ever ship a
    caller-token-less deployment's identity fields unredacted.
    """
    if identity_key is None:
        obj = _redact_identity_fields_flat(obj)
    else:
        obj = scrub_identity(obj, identity_key)
    return _scrub_paths_and_topology(obj)


def _redact_identity_fields_flat(obj: Any) -> Any:
    """Fallback used only when no identity key is available: blank the named
    fields outright rather than pseudonymize them."""
    if isinstance(obj, dict):
        return {
            key: (_IDENTITY_PLACEHOLDER if key in _IDENTITY_FIELD_NAMES and value
                  else _redact_identity_fields_flat(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_identity_fields_flat(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_redact_identity_fields_flat(item) for item in obj)
    return obj


def _scrub_paths_and_topology(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, dict):
        return {key: _scrub_paths_and_topology(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_scrub_paths_and_topology(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub_paths_and_topology(item) for item in obj)
    return obj


__all__ = [
    "scrub",
    "scrub_text",
    "scrub_identity",
    "pseudonymize_identity",
    "INTERNAL_TOPOLOGY_PLACEHOLDER",
]
