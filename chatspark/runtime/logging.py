import logging
import sys

from chatspark.runtime.config import settings

_CHATSPARK_HANDLER_ATTR = "_chatspark_default_handler"


def setup_logging(name: str = "chatspark"):
    logger = logging.getLogger(name)

    logger.disabled = False
    logger.propagate = True
    logger.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)

    if not any(getattr(handler, _CHATSPARK_HANDLER_ATTR, False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        setattr(handler, _CHATSPARK_HANDLER_ATTR, True)
        logger.addHandler(handler)

    return logger


logger = setup_logging()
