"""Typed errors and stable exit codes for amw-lite."""

from __future__ import annotations

EXIT_OK = 0
EXIT_EXECUTION_ERROR = 1
EXIT_VALIDATION_ERROR = 2
EXIT_SAFETY_BLOCK = 3
EXIT_DEPENDENCY_ERROR = 4


class AmwLiteError(Exception):
    """Domain, validation, safety, or runtime failure with stable exit code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "AMW_LITE_ERROR",
        exit_code: int = EXIT_EXECUTION_ERROR,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.details = details or {}
