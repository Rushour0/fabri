import logging

from fabri.paths import logs_dir


class _SessionFilter(logging.Filter):
    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = self.session_id
        return True


def configure_logging(session_id: str, verbose: bool = False) -> logging.Logger:
    """Logs to console (INFO, or DEBUG with verbose=True) and always to
    logs/<session_id>.log at DEBUG. The JSONL trace stays the structured
    machine-readable record of a run; this is the human-readable narrative
    of the same run, correlated by session_id."""
    logger = logging.getLogger("fabri")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.addFilter(_SessionFilter(session_id))

    fmt = logging.Formatter("%(asctime)s [%(session_id)s] %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(logs_dir() / f"{session_id}.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("fabri")
