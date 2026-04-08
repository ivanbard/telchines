from __future__ import annotations


class TelchinesError(Exception):
    """Base exception for user-facing Telchines errors."""


class ConfigError(TelchinesError):
    """Raised when a project config is missing or invalid."""


class ProjectNotInitializedError(ConfigError):
    """Raised when a command is run outside of a Telchines project."""


class AdapterExecutionError(TelchinesError):
    """Raised when an adapter cannot be executed safely."""


class ProviderError(TelchinesError):
    """Raised when a repair provider cannot generate or validate a response."""
