from software_factory.backends.base import Backend, BackendError
from software_factory.backends.claude_code import ClaudeCodeBackend
from software_factory.backends.litellm import LiteLLMBackend
from software_factory.backends.mock import MockBackend


def make_backend(name: str) -> Backend:
    if name == "litellm":
        return LiteLLMBackend()
    if name == "claude-code":
        return ClaudeCodeBackend()
    raise ValueError(f"unknown backend {name!r}: use litellm or claude-code (mock is built by tests and the demo)")


__all__ = ["Backend", "BackendError", "ClaudeCodeBackend", "LiteLLMBackend", "MockBackend", "make_backend"]
