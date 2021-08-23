import asyncio

from . import config

logger = config.logger


def process_message(msg):
    logger.debug("Received a message with topic %s.", msg.topic)

    if not msg.topic.endswith("buildsys.tag"):
        # Ignore any non-tagging messages
        return
