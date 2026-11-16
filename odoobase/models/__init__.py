"""Versioned native models with explicit selection and no default model exports."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

from . import v19_4


class ModelSet(Protocol):
    """The version and native TypedDict inventory supplied by a model module."""

    @property
    def ODOO_VERSION(self) -> str: ...

    @property
    def MODELS(self) -> Mapping[str, type[object]]: ...


_MODEL_SETS: Mapping[str, ModelSet] = MappingProxyType({v19_4.ODOO_VERSION: v19_4})
SUPPORTED_VERSIONS = tuple(_MODEL_SETS)


def for_version(version: str) -> ModelSet:
    """Select a bundled model module explicitly, for example in parametrized tests."""
    model_set = _MODEL_SETS.get(version)
    if model_set is None:
        raise ValueError(
            f"Unsupported Odoo model version {version!r}; "
            f"available versions: {', '.join(SUPPORTED_VERSIONS)}",
        )
    return model_set
