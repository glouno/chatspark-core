from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("chatspark-core")
except PackageNotFoundError:
    __version__ = "0.2.0"
