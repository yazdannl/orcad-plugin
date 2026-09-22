"""Deterministic content-addressed identities for rendered artifacts."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .catalog import QUALITY_PROFILES, SOURCE_REVISION
from .validation import validate_parameters


def cache_payload(object_name: str, params: Mapping[str, Any], engine_version: str,
                  quality_profile: str, *, source_revision: str = SOURCE_REVISION) -> dict[str, Any]:
    if quality_profile not in QUALITY_PROFILES:
        raise ValueError(f"unknown quality profile: {quality_profile}")
    return {
        "source_revision": source_revision,
        "engine_version": str(engine_version),
        "object": object_name,
        "params": validate_parameters(object_name, params),
        "quality_profile": quality_profile,
        "quality": QUALITY_PROFILES[quality_profile],
    }


def cache_key(*args: Any, source_revision: str = SOURCE_REVISION, **kwargs: Any) -> str:
    """Return a stable SHA-256 key.

    The preferred call is ``cache_key(object, params, engine, profile)``. The
    legacy source-first five-positional form remains accepted for bridge code:
    ``cache_key(source_revision, engine, object, params, profile)``.
    """
    if kwargs:
        object_name = kwargs.pop("object_name")
        params = kwargs.pop("params")
        engine_version = kwargs.pop("engine_version")
        quality_profile = kwargs.pop("quality_profile", kwargs.pop("quality", "balanced"))
        source_revision = kwargs.pop("source_revision", source_revision)
        if kwargs:
            raise TypeError(f"unexpected cache-key arguments: {', '.join(kwargs)}")
    elif len(args) == 4:
        object_name, params, engine_version, quality_profile = args
    elif len(args) == 5:
        source_revision, engine_version, object_name, params, quality_profile = args
    else:
        raise TypeError("cache_key expects 4 or 5 positional arguments")
    payload = cache_payload(str(object_name), params, str(engine_version), str(quality_profile), source_revision=str(source_revision))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
