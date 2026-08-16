"""Import shim: ``siren.app`` is the conventional module name across the
federation. Importing it must not boot a second service as a side effect, so
it re-exports the single app instance rather than building another."""

from .service import app, create_app  # noqa: F401
