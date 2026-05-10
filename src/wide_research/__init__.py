"""Wide Research — parallel subtasks in Modal sandboxes with OpenAI Agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wide-research")
except PackageNotFoundError:
    __version__ = "0.0.0"
