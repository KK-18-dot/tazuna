"""Exit-code contract shared by every command.

0   done
1   provider failed (or the result could not be recorded)
2   usage / configuration error
3   safety boundary violated (bad --dir, forbidden output path, recursion, secret-like env)
4   provider exited 0 but produced (almost) no output
124 timeout
130 cancelled by SIGINT (the provider's process group was terminated; HANDOFF and ledger written)
143 cancelled by SIGTERM or SIGHUP (same)
"""

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_SAFETY = 3
EXIT_EMPTY = 4
EXIT_TIMEOUT = 124
EXIT_CANCELLED_INT = 130
EXIT_CANCELLED_TERM = 143


class TazunaError(Exception):
    """Base error; ``code`` is the process exit code."""

    code = EXIT_FAILED

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class ConfigError(TazunaError):
    code = EXIT_USAGE


class UsageError(TazunaError):
    code = EXIT_USAGE


class SafetyError(TazunaError):
    code = EXIT_SAFETY
