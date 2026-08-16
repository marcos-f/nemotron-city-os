"""The dataset discovery surface: ``GET /datasets`` and ``GET /datasets/{id}``.

Read-only by construction. Discovery tells an operator or an agent what data a
component is standing on; it is not a second way to change it. The only
mutating route here is ``POST /datasets/reload``, which re-reads the declared
registry file from disk and refuses a bad one exactly as the effect config
loader does — it cannot set a dataset's source or mode from the request body.

Lives in its own module, and attaches with a single call, so the discovery
surface can be added to a component without reopening its ``app.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import POLICY_SOURCE_ALLOWLIST, ConfigRefusal
from .datasets import DESIGNED_NOT_BUILT, DatasetRegistry, DatasetRegistryStore

#: Where the registry file lives, overridable without a code change.
DEFAULT_REGISTRY_ENV = "DATASETS_CONFIG"
DEFAULT_REGISTRY_PATH = "config/datasets.yaml"


def registry_path(component_env: Optional[str] = None) -> Path:
    """Resolve the registry path from the environment.

    Checks the component-specific variable first (``SIREN_DATASETS``,
    ``DOCKET_DATASETS``, ...) then the shared ``DATASETS_CONFIG``, then the
    conventional in-repo location. Changing a dataset's source or mode is
    therefore a config edit or an env var, never a code change.
    """
    if component_env and os.environ.get(component_env):
        return Path(os.environ[component_env])
    return Path(os.environ.get(DEFAULT_REGISTRY_ENV, DEFAULT_REGISTRY_PATH))


def build_router() -> APIRouter:
    router = APIRouter(tags=["datasets"])

    def store(request: Request) -> DatasetRegistryStore:
        return request.app.state.dataset_registry_store

    @router.get("/datasets")
    async def list_datasets(request: Request) -> dict[str, Any]:
        """Enumerate every dataset this component consumes, with provenance."""
        registry = store(request).current
        body = registry.as_dict()
        body["refusal"] = getattr(request.app.state, "dataset_refusal", None)
        # An unavailable dataset is reported, not omitted. Callers that render
        # a count can say how much of what they list is actually reachable.
        body["unavailable"] = [e.id for e in registry.entries if e.unavailable]
        body["honesty_note"] = DESIGNED_NOT_BUILT
        return body

    @router.get("/datasets/{id}")
    async def get_dataset(id: str, request: Request) -> dict[str, Any]:
        """Inspect one dataset: source, licence, provenance, as-of, mode."""
        entry = store(request).current.get(id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown dataset {id}")
        body = entry.as_dict()
        if entry.unavailable:
            # Never an empty payload dressed as a result.
            body["unavailable_reason"] = DESIGNED_NOT_BUILT
        return body

    @router.post("/datasets/reload")
    async def reload_datasets(
        request: Request, body: dict[str, Any] = Body(default_factory=dict)
    ):
        """Reload the registry atomically. A refusal names file, entry and line,
        and the previously loaded registry keeps running untouched."""
        registry_store = store(request)
        path = body.get("path") or request.app.state.dataset_registry_path
        allowlist = getattr(request.app.state, "dataset_registry_allowlist", None)
        try:
            registry = registry_store.reload(path, allowlist)
        except ConfigRefusal as refusal:
            payload = refusal.as_dict()
            # A path outside the operator's nominated directories is a 403,
            # exactly as it is on /config/reload — the same policy id, the
            # same status, so an operator learns one rule and not two.
            outside = payload["policy"] == POLICY_SOURCE_ALLOWLIST
            request.app.state.dataset_refusal = payload
            return JSONResponse(status_code=403 if outside else 422, content={
                **payload,
                "allowlist": [str(p) for p in (allowlist or [])],
                "active_registry": registry_store.current.as_dict(),
                "message_ui": "Dataset registry refused — "
                              "previous registry still running.",
            })
        request.app.state.dataset_refusal = None
        return {"refused": False, "registry": registry.as_dict()}

    return router


def attach_datasets(
    app: FastAPI,
    *,
    component: str,
    ledger=None,
    path: Optional[str | Path] = None,
    component_env: Optional[str] = None,
    allowed_dirs: Optional[list[Path]] = None,
) -> DatasetRegistryStore:
    """Mount the dataset discovery surface on ``app``.

    A registry that refuses at boot leaves the component running with an EMPTY
    registry and the refusal on display — the same fail-closed posture the
    effect loader takes. It does not fall back to a previous file on disk and
    it does not guess.
    """
    resolved = Path(path) if path is not None else registry_path(component_env)
    store = DatasetRegistryStore(ledger=ledger, component=component)
    app.state.dataset_registry_store = store
    app.state.dataset_registry_path = str(resolved)
    app.state.dataset_refusal = None
    # Confine the reload surface to the directory the registry itself lives in,
    # plus anything the operator nominated — the same confinement, and the same
    # policy id, that /config/reload applies. Boot is exempt for the same
    # reason it is there: the path came from the operator, not from a request.
    app.state.dataset_registry_allowlist = (
        list(allowed_dirs) if allowed_dirs is not None
        else [resolved.expanduser().resolve().parent]
    )

    if resolved.exists():
        try:
            store.reload(resolved)
        except ConfigRefusal as refusal:
            app.state.dataset_refusal = refusal.as_dict()
    else:
        app.state.dataset_refusal = {
            "refused": True,
            "file": str(resolved),
            "rule": None,
            "line": None,
            "policy": "dataset-registry-must-exist",
            "message": "no dataset registry declared at this path",
            "detail": f"{resolved}: no dataset registry declared at this path",
        }

    app.include_router(build_router())
    return store


__all__ = [
    "DEFAULT_REGISTRY_ENV",
    "DEFAULT_REGISTRY_PATH",
    "DatasetRegistry",
    "attach_datasets",
    "build_router",
    "registry_path",
]
