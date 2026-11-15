import builtins
import functools
import logging
import sys
import threading
import time

from .api import make_compat_message_v1
from .util import ANSI

logger = logging.getLogger(f"{__name__.split('.')[0]}")
console = logging.getLogger(f"{logger.name}.console")  # captured stdout/stderr

_input = builtins.input
_stdout = sys.stdout
_stderr = sys.stderr

colors = {
    "DEBUG": ANSI.green,
    "INFO": ANSI.cyan,
    "WARNING": ANSI.yellow,
    "ERROR": ANSI.red,
    "CRITICAL": ANSI.purple,
}
styles = {
    "DEBUG": " 💬 ",
    "INFO": " 🚀 ",
    "WARNING": " 🚨 ",
    "ERROR": " ⛔ ",
    "CRITICAL": " 🚫 ",
}


class ColorFormatter(logging.Formatter):
    def format(self, record):
        prefix = ANSI.bold + ANSI.cyan + f"{__name__.split('.')[0]}:" + ANSI.reset
        color = colors.get(record.levelname, "")
        style = styles.get(record.levelname, "")

        # TODO: remove legacy compat
        if record.msg.startswith("Operation"):
            prefix = "\n" + prefix
        
        return f"{prefix}{color}{style}{super().format(record)}{ANSI.reset}"


class ConsoleHandler:
    """Stands in for sys.stdout / sys.stderr while a run is live: every line
    is recorded for the run, then written to the stream it replaced."""

    _capturing = threading.local()  # shared by both streams

    def __init__(
        self, logger, queue, level=logging.INFO, stream=sys.stdout, type="stdout"
    ):
        self.logger = logger
        self.queue = queue
        self.level = level
        self.stream = stream
        self.type = type
        self.count = 0
        self.active = True

    def write(self, buf: str) -> int:
        # Recording a line can itself write to stdout/stderr: a logging handler
        # the host created after init holds this object as its stream, and so
        # does logging's own error reporting. Such a nested write is passed
        # through unrecorded, or it would record itself without end. So is
        # anything written once the run is over, by whoever kept a reference,
        # and anything from the package's own threads (see `uncaptured`).
        local = ConsoleHandler._capturing
        if self.active and not getattr(local, "busy", False):
            local.busy = True
            try:
                for line in buf.splitlines():
                    if line:  # do not log empty lines
                        self.count += 1
                        self.queue.put(
                            make_compat_message_v1(
                                self.level, line, time.time(), self.count
                            )
                        )
                        self.logger.log(self.level, line)
            finally:
                local.busy = False
        self.stream.write(buf)
        self.stream.flush()
        return len(buf)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, attr):
        if attr == "stream":  # not set yet, e.g. while being copied
            raise AttributeError(attr)
        return getattr(self.stream, attr)


def uncaptured(worker):
    """For the package's own threads: nothing they write is recorded as the
    run's console output. Recording it feeds itself where the host's logging
    prints to the captured streams: sending a line logs a line (the HTTP
    library's request log, or our own report of a failed send), which is then
    captured and sent, and the run can never finish sending."""

    @functools.wraps(worker)
    def run(*args, **kwargs):
        ConsoleHandler._capturing.busy = True
        return worker(*args, **kwargs)

    return run


def _prints_to_console(handler) -> bool:
    """Whether a logging handler writes to this process's stdout or stderr."""
    if isinstance(handler, logging.FileHandler):
        return False
    stream = getattr(handler, "stream", None)
    if stream is None:  # rich's handler writes to its console's file
        stream = getattr(getattr(handler, "console", None), "file", None)
    while isinstance(stream, ConsoleHandler):
        stream = stream.stream
    if stream is None:
        return False
    ours = [sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__, _stdout, _stderr]
    for i, std in enumerate(ours):
        while isinstance(std, ConsoleHandler):
            std = ours[i] = std.stream
    if any(stream is std for std in ours):
        return True
    try:
        return stream.fileno() in (1, 2)
    except Exception:  # not backed by a file descriptor
        return False


class HostHandlers(logging.Handler):
    """Stands in for propagation on one of the package's loggers: passes each
    record to the handlers the host has set up above it, except those that
    print to stdout or stderr. The package prints its messages itself, so such
    a handler would show every message twice, and with console capture on
    would put the second copy into the run's own console log."""

    def __init__(self, logger):
        super().__init__()
        self.logger = logger

    def emit(self, record):
        source = self.logger.parent
        while source is not None:
            for handler in source.handlers:
                if (
                    record.levelno >= handler.level
                    and handler not in self.logger.handlers
                    and not _prints_to_console(handler)
                ):
                    handler.handle(record)
            if not source.propagate:
                break
            source = source.parent


def stream_formatter(settings):
    if settings.x_log_level <= logging.DEBUG:
        return ColorFormatter(
            "%(asctime)s.%(msecs)03d %(levelname)-8s %(threadName)-10s:%(process)d "
            "[%(filename)s:%(funcName)s():%(lineno)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        return ColorFormatter(
            "%(asctime)s | %(message)s",
            # "%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S",
        )


def input_hook(prompt="", logger=None):
    content = _input(prompt)
    logger.warning(f"{prompt}{content}")
    return content


def setup_logger(settings, logger, console=None) -> None:
    # TODO: capture stdout through rich
    if settings._nb_colab():
        rlogger = logging.getLogger()
        for h in rlogger.handlers[:]:  # iter root handlers
            rlogger.removeHandler(h)

    logger.setLevel(settings.x_log_level)

    if not any(isinstance(h, HostHandlers) for h in logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(stream_formatter(settings))
        logger.addHandler(stream_handler)
        # We print our own messages, so the host's logging must not print
        # them a second time; everything else of the host's still gets them.
        logger.addHandler(HostHandlers(logger))
        logger.propagate = False

    if settings._op_id and not settings.disable_console:
        if isinstance(sys.stdout, ConsoleHandler):  # already capturing
            return
        logger, console = setup_logger_file(settings, logger, console)


def teardown_logger(logger, console=None):
    for h in logger.handlers[:]:
        logger.removeHandler(h)
        h.close()
    logger.propagate = True
    if console:
        builtins.input = _input
        for name in ("stdout", "stderr"):
            # Unwrap only what is ours. Whatever replaced the stream since
            # (a test's capture, a progress bar) stays; if it writes through
            # the wrapper, the wrapper now only passes the text on.
            wrapper = getattr(sys, name)
            while isinstance(wrapper, ConsoleHandler):
                wrapper.active = False
                wrapper = wrapper.stream
                setattr(sys, name, wrapper)
        for h in console.handlers[:]:
            console.removeHandler(h)
            h.close()


def setup_logger_file(settings, logger, console):
    console.setLevel(logging.DEBUG)
    # Captured lines go to the run's files and nowhere else. Passed up to the
    # root logger they would reach the host's handlers, which print them to
    # the very streams they were captured from.
    console.propagate = False

    file_handler = logging.FileHandler(f"{settings.get_dir()}/{settings.tag}.log")
    file_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(threadName)-10s:%(process)d "
        "[%(filename)s:%(funcName)s():%(lineno)s] %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    file_handler = logging.FileHandler(f"{settings.get_dir()}/sys.log")
    file_formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)
    console.addHandler(file_handler)  # TODO: fix slow file writes

    if settings.mode == "debug":
        builtins.input = lambda prompt="": input_hook(prompt, logger=console)
    sys.stdout = ConsoleHandler(
        console, settings.message, logging.INFO, sys.stdout, "stdout"
    )
    sys.stderr = ConsoleHandler(
        console, settings.message, logging.ERROR, sys.stderr, "stderr"
    )

    return logger, console
