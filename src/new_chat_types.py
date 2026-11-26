"""Internal New chat adapter contract; never accepts caller-supplied native paths."""
from __future__ import annotations

from dataclasses import dataclass

MANAGED_PERMISSION_TIMEOUT_SECONDS = 120
# Where Claude run by Motet may start (step 8b.5 added Windows). Linux is deliberately absent. The one definition the
# manager, its supervisor and the server all read.
MANAGED_CLAUDE_PLATFORMS = ('darwin', 'win32')


@dataclass(frozen=True)
class FrozenFolder:
    id: str
    revision: str
    label: str
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class Capability:
    available: bool
    code: str = ""
    message: str = ""
    version: str = ""
    supports_work: bool = False
    supports_blank: bool = True
    ownership: str = 'desktop'


@dataclass(frozen=True)
class VerifiedIdentity:
    native_id: str
    cwd: str
    source_path: str
    source_device: int
    source_inode: int
    adapter_version: str
    folder_device: int = 0
    folder_inode: int = 0
    provider: str = 'codex'


@dataclass(frozen=True)
class OpenResult:
    outcome: str  # accepted, unknown, rejected; never proof of visible UI
    code: str = ""
    message: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    outcome: str
    turn_id: str | None = None
    code: str = ""
    receipt_kind: str = 'native_turn'


class NewChatError(Exception):
    """Safe allowlisted error, not an arbitrary native exception string."""
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
