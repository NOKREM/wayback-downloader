"""Progress reporting.

The service layer depends on the :class:`ProgressReporter` interface rather
than on Rich directly, so the same code runs unchanged under a progress bar, in
quiet mode, or embedded in another application.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from typing import Iterator, Protocol

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from wayback_downloader.utils.logger import console


class ProgressTask(Protocol):
    """A single unit of work whose completion can be advanced."""

    def advance(self, amount: int = 1) -> None:
        """Report that ``amount`` more steps have finished."""
        ...


class _NullTask:
    """A task handle that discards every update."""

    def advance(self, amount: int = 1) -> None:
        """Discard the update."""


class ProgressReporter(Protocol):
    """Creates progress tasks for long-running operations."""

    def task(self, description: str, total: int) -> AbstractContextManager[ProgressTask]:
        """Open a progress task as a context manager."""
        ...


class NullProgress:
    """A reporter that renders nothing, used in quiet and library modes."""

    @contextmanager
    def task(self, description: str, total: int) -> Iterator[ProgressTask]:
        """Yield a no-op task handle."""
        yield _NullTask()


class _RichTask:
    """Adapts a Rich task id to the :class:`ProgressTask` interface."""

    def __init__(self, progress: Progress, task_id: int) -> None:
        """Bind the handle to a Rich progress instance and task id."""
        self._progress = progress
        self._task_id = task_id

    def advance(self, amount: int = 1) -> None:
        """Advance the underlying Rich task."""
        self._progress.advance(self._task_id, amount)


class RichProgress:
    """Renders a Rich progress bar for each task."""

    @contextmanager
    def task(self, description: str, total: int) -> Iterator[ProgressTask]:
        """Show a live progress bar for the duration of the task."""
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=32),
            MofNCompleteColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        with progress:
            task_id = progress.add_task(description, total=max(total, 1))
            yield _RichTask(progress, task_id)
