"""Generation-safe image loading with bounded negative caching.

Workers decode bytes to :class:`QImage`; only the GUI-thread handler creates
QPixmaps.  Per-photo retry state prevents a missing original rendition from
being requested whenever a cached fallback re-enters a prefetch radius.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

from observation_workbench.api.client import INatClient
from observation_workbench.models import StudyObservation, StudyPhoto
from observation_workbench.services.image_cache import ImageCache

log = logging.getLogger(__name__)

# Descending quality. "square" is the floor: it is only ever produced for a
# photo whose URL carries no substitutable size token (see
# StudyPhoto.candidate_size_urls), and it must be a KNOWN rank rather than an
# unrecognized label — the helpers below fall back to "return everything" on an
# unknown size, which would make such a photo redownload on every navigation,
# and the memory/disk lookups iterate this tuple, so an absent size would never
# be found in cache.
_SIZE_ORDER = ("original", "large", "medium", "small", "square")
_TOTAL_FAILURE_COOLDOWN_SECONDS = 90.0
_ORIGINAL_UPGRADE_COOLDOWN_SECONDS = 300.0
_MAX_PHOTO_STATES = 2_000
_MAX_PARTIAL_FAILURES = 12
# Neighboring-observation prefetch still targets original size (kept, by
# design, for image quality), but a wide Identify radius can otherwise queue
# many simultaneous full-original downloads. Bounding how many background
# (non-focused) prefetch/upgrade workers run at once keeps the media budget
# in check without shrinking the size any single photo is fetched at.
_MAX_CONCURRENT_BACKGROUND_PREFETCH = 3


class ImageRequestMode(str, Enum):
    """Intent for an image request; only explicit retry bypasses cooldowns."""

    NORMAL = "normal"
    BACKGROUND_PREFETCH = "background_prefetch"
    EXPLICIT_RETRY = "explicit_retry"
    ORIGINAL_UPGRADE = "original_upgrade"


@dataclass(frozen=True)
class ImageAttemptFailure:
    """One failed waterfall candidate, suitable for a local Details view."""

    photo_id: int
    url: str
    size: str
    candidate_number: int
    exception_type: str
    message: str
    status_code: int | None
    timestamp: str


@dataclass(frozen=True)
class ImageFailure:
    photo_id: int
    attempts: tuple[ImageAttemptFailure, ...]

    @property
    def message(self) -> str:
        return "All image sizes failed"


@dataclass(frozen=True)
class ImageLoadDiagnostics:
    """Read-only per-photo retry/partial-failure state for UI presentation."""

    photo_id: int
    loaded_size: str | None
    partial_failures: tuple[ImageAttemptFailure, ...]
    complete_failure: ImageFailure | None
    next_automatic_retry_at: float
    next_original_upgrade_at: float
    last_failure_time: float


@dataclass
class _PhotoLoadState:
    loaded_size: str | None = None
    partial_failures: list[ImageAttemptFailure] = field(default_factory=list)
    complete_failure: ImageFailure | None = None
    next_automatic_retry_at: float = 0.0
    next_original_upgrade_at: float = 0.0
    last_failure_time: float = 0.0


@dataclass(frozen=True)
class _ImageRequest:
    token: int
    generation: int
    observation_id: int
    observation_index: int
    photo_id: int
    photo_index: int
    request_mode: ImageRequestMode
    previous_loaded_size: str | None
    counts_against_background_cap: bool = False
    focus_exempt: bool = False
    started_at: float = 0.0


@dataclass(frozen=True)
class _ImageTerminalResult:
    """The one terminal worker-to-prefetcher transition for a request."""

    request: _ImageRequest
    loaded_size: str | None = None
    image: QImage | None = None
    attempts: tuple[ImageAttemptFailure, ...] = ()
    cancelled: bool = False
    source: str = "none"


class _ImageWorkerSignals(QObject):
    terminal = Signal(object)


class _ImageWorker(QRunnable):
    """Download selected waterfall candidates without creating QPixmaps."""

    def __init__(
        self,
        request: _ImageRequest,
        candidates: tuple[tuple[str, str], ...],
        client: INatClient,
        disk_cache: ImageCache,
        is_cancelled: Callable[[], bool],
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._request = request
        self._candidates = candidates
        self._client = client
        self._disk_cache = disk_cache
        self._is_cancelled = is_cancelled
        self.signals = _ImageWorkerSignals()

    def run(self) -> None:
        try:
            result = self._load()
        except Exception as exc:  # A worker must always have one terminal result.
            attempt = _failure_from_exception(
                self._request.photo_id,
                url="",
                size="all",
                candidate_number=0,
                exc=exc,
            )
            result = _ImageTerminalResult(self._request, attempts=(attempt,))
        try:
            self.signals.terminal.emit(result)
        except RuntimeError as exc:
            log.debug(
                "Dropped terminal image result for photo %d: %s",
                self._request.photo_id,
                exc,
            )

    def _load(self) -> _ImageTerminalResult:
        if self._is_cancelled():
            return _ImageTerminalResult(self._request, cancelled=True)

        failures: list[ImageAttemptFailure] = []
        if not self._candidates:
            failures.append(
                ImageAttemptFailure(
                    photo_id=self._request.photo_id,
                    url="",
                    size="all",
                    candidate_number=0,
                    exception_type="ValueError",
                    message="The photograph has no usable image URL",
                    status_code=None,
                    timestamp=_utc_timestamp(),
                )
            )

        for candidate_number, (size, url) in enumerate(self._candidates, start=1):
            if self._is_cancelled():
                return _ImageTerminalResult(self._request, cancelled=True)

            cached_bytes = self._disk_cache.get(self._request.photo_id, size)
            if cached_bytes is not None:
                image = _decode_image(cached_bytes)
                if not image.isNull():
                    if self._is_cancelled():
                        return _ImageTerminalResult(self._request, cancelled=True)
                    return _ImageTerminalResult(
                        self._request,
                        loaded_size=size,
                        image=image,
                        attempts=tuple(failures),
                        source="disk",
                    )
                failures.append(
                    ImageAttemptFailure(
                        photo_id=self._request.photo_id,
                        url=url,
                        size=size,
                        candidate_number=candidate_number,
                        exception_type="CacheDecodeError",
                        message="Cached image bytes could not be decoded; cache entry removed",
                        status_code=None,
                        timestamp=_utc_timestamp(),
                    )
                )
                self._disk_cache.remove(self._request.photo_id, size)

            if self._is_cancelled():
                return _ImageTerminalResult(self._request, cancelled=True)
            try:
                data = self._client.download_image(url)
            except Exception as exc:
                failures.append(
                    _failure_from_exception(
                        self._request.photo_id,
                        url,
                        size,
                        candidate_number,
                        exc,
                    )
                )
                continue

            if self._is_cancelled():
                return _ImageTerminalResult(self._request, cancelled=True)
            image = _decode_image(data)
            if image.isNull():
                failures.append(
                    ImageAttemptFailure(
                        photo_id=self._request.photo_id,
                        url=url,
                        size=size,
                        candidate_number=candidate_number,
                        exception_type="ImageDecodeError",
                        message="Downloaded image bytes could not be decoded",
                        status_code=None,
                        timestamp=_utc_timestamp(),
                    )
                )
                continue

            self._disk_cache.put(
                self._request.photo_id,
                size,
                data,
                _detect_extension(data),
            )
            if self._is_cancelled():
                return _ImageTerminalResult(self._request, cancelled=True)
            return _ImageTerminalResult(
                self._request,
                loaded_size=size,
                image=image,
                attempts=tuple(failures),
                source="network",
            )

        return _ImageTerminalResult(self._request, attempts=tuple(failures))


class ImagePrefetcher(QObject):
    """Per-window image memory cache with generation-safe retry eligibility.

    A complete waterfall failure uses a 90-second automatic cooldown.  When a
    fallback succeeds after higher-quality candidates fail, only those upgrade
    candidates are retried, after a five-minute cooldown or explicit retry.
    """

    # Backward-compatible signals consumed by MainWindow.
    image_ready = Signal(int, int, int, int, QPixmap)
    image_ready_detailed = Signal(int, int, int, int, str, QPixmap)
    image_failed = Signal(int, str, str)
    image_failed_detailed = Signal(object)
    request_state_changed = Signal(
        int, object
    )  # active count, immutable photo-ID tuple
    image_diagnostics_changed = Signal(int)

    def __init__(
        self,
        client: INatClient,
        disk_cache: ImageCache,
        max_memory_bytes: int = 256 * 1024 * 1024,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._disk_cache = disk_cache
        self._max_memory_bytes = max_memory_bytes
        self._pool = QThreadPool.globalInstance()
        self._memory: OrderedDict[tuple[int, str], QPixmap] = OrderedDict()
        self._memory_bytes = 0
        self._generation = 0
        self._next_request_token = 0
        self._observations: list[StudyObservation] = []
        self._in_flight: dict[int, _ImageRequest] = {}
        self._request_for_photo: dict[int, int] = {}
        self._photo_states: OrderedDict[int, _PhotoLoadState] = OrderedDict()
        self._live_signals: set[_ImageWorkerSignals] = set()
        self._superseded_tokens: set[int] = set()
        self._background_prefetch_in_flight = 0
        self._focused_photo_id: int | None = None

    def set_observations(
        self,
        observations: list[StudyObservation],
        radius: int = 3,
    ) -> None:
        """Begin a new generation and release old queue/retry state."""
        del radius  # Retained for the original study-viewer public signature.
        self._generation += 1
        self._observations = list(observations)
        self._memory.clear()
        self._memory_bytes = 0
        self._in_flight.clear()
        self._request_for_photo.clear()
        self._photo_states.clear()
        self._superseded_tokens.clear()
        self._background_prefetch_in_flight = 0
        self._focused_photo_id = None
        self._emit_request_state()

    def clear(self) -> None:
        """Invalidate workers and free this window's memory and retry metadata."""
        self._generation += 1
        self._observations.clear()
        self._memory.clear()
        self._memory_bytes = 0
        self._in_flight.clear()
        self._request_for_photo.clear()
        self._photo_states.clear()
        self._superseded_tokens.clear()
        self._background_prefetch_in_flight = 0
        self._focused_photo_id = None
        self._emit_request_state()

    def append_observations(self, observations: list[StudyObservation]) -> None:
        """Extend the current generation without discarding warm image state."""
        self._observations.extend(observations)

    def update_position(
        self,
        obs_idx: int,
        photo_idx: int = 0,
        radius: int = 3,
        *,
        direction: int = 0,
        include_secondary: bool = True,
    ) -> None:
        """Prefetch likely next observations without lowering image quality."""
        del photo_idx
        if not self._observations:
            return

        step = 1 if direction >= 0 else -1
        if direction:
            indexes = [
                obs_idx + distance * step
                for distance in range(1, radius + 1)
                if 0 <= obs_idx + distance * step < len(self._observations)
            ]
            indexes.extend(
                obs_idx - distance * step
                for distance in range(1, radius + 1)
                if 0 <= obs_idx - distance * step < len(self._observations)
            )
        else:
            indexes = [
                index
                for distance in range(1, radius + 1)
                for index in (obs_idx + distance, obs_idx - distance)
                if 0 <= index < len(self._observations)
            ]

        # First photos are the hot path for Left/Right review. Queue all of
        # those before secondary photos, while retaining original-first URL
        # selection and the bounded background worker budget.
        for priority_offset, index in enumerate(indexes):
            observation = self._observations[index]
            if observation.photos:
                self.request_photo(
                    observation.photos[0],
                    observation.obs_id,
                    index,
                    0,
                    priority=5 - priority_offset,
                    request_mode=ImageRequestMode.BACKGROUND_PREFETCH,
                )
        if not include_secondary:
            return
        secondary_indexes = indexes + [obs_idx]
        for priority_offset, index in enumerate(secondary_indexes):
            observation = self._observations[index]
            for image_index, photo in enumerate(observation.photos[1:], start=1):
                self.request_photo(
                    photo,
                    observation.obs_id,
                    index,
                    image_index,
                    priority=-5 - priority_offset - image_index,
                    request_mode=ImageRequestMode.BACKGROUND_PREFETCH,
                )

    def prefetch_identify_position(
        self,
        obs_idx: int,
        photo_idx: int = 0,
        radius: int = 3,
        direction: int = 0,
    ) -> None:
        """Identify prefetch prioritizes the visible photo without bypassing cooldowns."""
        if not 0 <= obs_idx < len(self._observations):
            return
        current = self._observations[obs_idx]
        focused_photo_id: int | None = None
        if current.photos:
            selected_index = min(max(0, photo_idx), len(current.photos) - 1)
            focused_photo_id = current.photos[selected_index].photo_id
        self._set_focused_photo(focused_photo_id)
        if current.photos:
            self.request_photo(
                current.photos[selected_index],
                current.obs_id,
                obs_idx,
                selected_index,
                priority=100,
                request_mode=ImageRequestMode.BACKGROUND_PREFETCH,
                # The focused photo must stay instant regardless of how many
                # neighboring background prefetches are already in flight.
                background_cap_exempt=True,
            )
            for image_index, photo in enumerate(current.photos):
                if image_index != selected_index:
                    self.request_photo(
                        photo,
                        current.obs_id,
                        obs_idx,
                        image_index,
                        priority=90,
                        request_mode=ImageRequestMode.BACKGROUND_PREFETCH,
                    )

        offsets = (1, -1) if direction >= 0 else (-1, 1)
        for distance in range(1, radius + 1):
            for offset in offsets:
                index = obs_idx + distance * offset
                if not 0 <= index < len(self._observations):
                    continue
                observation = self._observations[index]
                for image_index, photo in enumerate(observation.photos):
                    self.request_photo(
                        photo,
                        observation.obs_id,
                        index,
                        image_index,
                        priority=80 - distance * 10 - image_index,
                        request_mode=ImageRequestMode.BACKGROUND_PREFETCH,
                    )

    def request_photo(
        self,
        photo: StudyPhoto,
        observation_id: int,
        observation_index: int,
        photo_index: int,
        *,
        priority: int = 0,
        request_mode: ImageRequestMode = ImageRequestMode.NORMAL,
        retry: bool | None = None,
        background_cap_exempt: bool = False,
    ) -> bool:
        """Request a photo and return whether a new worker was started.

        ``retry`` remains accepted for existing callers; ``True`` maps to an
        explicit retry while request modes make ordinary prefetch intent clear.
        ``background_cap_exempt`` lets one focused-photo request bypass the
        concurrent-background-prefetch budget cap (see
        ``_MAX_CONCURRENT_BACKGROUND_PREFETCH``); ordinary background
        prefetch/upgrade requests remain subject to it.
        """
        if retry is True:
            request_mode = ImageRequestMode.EXPLICIT_RETRY
        return self._start_request(
            photo,
            observation_id,
            observation_index,
            photo_index,
            priority,
            request_mode,
            background_cap_exempt=background_cap_exempt,
        )

    def is_request_in_flight(self, photo_id: int) -> bool:
        return photo_id in self._request_for_photo

    def active_request_mode(self, photo_id: int) -> ImageRequestMode | None:
        token = self._request_for_photo.get(photo_id)
        request = self._in_flight.get(token) if token is not None else None
        return request.request_mode if request is not None else None

    def failure_for_photo(self, photo_id: int) -> ImageFailure | None:
        state = self._state_for(photo_id, create=False)
        return state.complete_failure if state is not None else None

    def diagnostics_for_photo(self, photo_id: int) -> ImageLoadDiagnostics | None:
        state = self._state_for(photo_id, create=False)
        if state is None:
            return None
        return ImageLoadDiagnostics(
            photo_id=photo_id,
            loaded_size=state.loaded_size,
            partial_failures=tuple(state.partial_failures),
            complete_failure=state.complete_failure,
            next_automatic_retry_at=state.next_automatic_retry_at,
            next_original_upgrade_at=state.next_original_upgrade_at,
            last_failure_time=state.last_failure_time,
        )

    def can_explicit_retry(self, photo_id: int) -> bool:
        state = self._state_for(photo_id, create=False)
        return bool(state and (state.complete_failure or state.partial_failures))

    def replace_observation(self, observation: StudyObservation) -> bool:
        """Enrich an observation by ID without changing this queue generation."""
        for index, existing in enumerate(self._observations):
            if existing.obs_id == observation.obs_id:
                self._observations[index] = observation
                return True
        return False

    def get_cached(self, photo_id: int) -> QPixmap | None:
        cached = self.get_best_cached(photo_id)
        return cached[0] if cached is not None else None

    def get_cached_size(self, photo_id: int, size: str) -> QPixmap | None:
        key = (photo_id, size)
        pixmap = self._memory.get(key)
        if pixmap is not None:
            self._memory.move_to_end(key)
        return pixmap

    def get_best_memory_cached(self, photo_id: int) -> tuple[QPixmap, str] | None:
        """Return the best in-memory rendition without disk I/O or decoding."""
        for size in _SIZE_ORDER:
            pixmap = self.get_cached_size(photo_id, size)
            if pixmap is not None:
                return pixmap, size
        return None

    def get_best_cached(self, photo_id: int) -> tuple[QPixmap, str] | None:
        """Return the best valid cached rendition, invalidating corrupt bytes."""
        for size in _SIZE_ORDER:
            pixmap = self.get_cached_size(photo_id, size)
            if pixmap is not None:
                return pixmap, size
            cached_bytes = self._disk_cache.get(photo_id, size)
            if cached_bytes is None:
                continue
            image = _decode_image(cached_bytes)
            if image.isNull():
                self._disk_cache.remove(photo_id, size)
                continue
            pixmap = QPixmap.fromImage(image)
            self._store_memory(photo_id, size, pixmap)
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "Image cache hit source=disk photo=%d size=%s pixels=%dx%d",
                    photo_id,
                    size,
                    pixmap.width(),
                    pixmap.height(),
                )
            return pixmap, size
        return None

    def set_max_memory(self, bytes_limit: int) -> None:
        self._max_memory_bytes = max(0, bytes_limit)
        self._evict_memory_if_needed()

    def _start_request(
        self,
        photo: StudyPhoto,
        observation_id: int,
        observation_index: int,
        photo_index: int,
        priority: int,
        request_mode: ImageRequestMode,
        *,
        background_cap_exempt: bool = False,
    ) -> bool:
        existing_token = self._request_for_photo.get(photo.photo_id)
        existing_request = (
            self._in_flight.get(existing_token) if existing_token is not None else None
        )
        if existing_token is not None and existing_request is None:
            # Heal a stale reverse index defensively instead of allowing it to
            # suppress every future request for this photo.
            self._request_for_photo.pop(photo.photo_id, None)
            existing_token = None
        if existing_token is not None:
            already_retrying = (
                existing_request is not None
                and existing_request.request_mode == ImageRequestMode.EXPLICIT_RETRY
            )
            explicit_supersede = (
                request_mode == ImageRequestMode.EXPLICIT_RETRY and not already_retrying
            )
            focus_promotion = (
                background_cap_exempt
                and request_mode == ImageRequestMode.BACKGROUND_PREFETCH
                and existing_request is not None
                and existing_request.counts_against_background_cap
            )
            if not explicit_supersede and not focus_promotion:
                return False
        state = self._state_for(photo.photo_id, create=True)
        assert state is not None
        # In particular, perform this cache check before superseding an old
        # request.  A worker may just have populated the shared disk cache; if
        # that makes a retry unnecessary, its pending terminal result remains
        # authoritative and will still publish image/request state.
        candidates, previous_loaded_size = self._eligible_candidates(
            photo, state, request_mode
        )
        if not candidates:
            return False
        warming_memory_cache = bool(
            previous_loaded_size is not None
            and candidates[0][0] == previous_loaded_size
        )
        if existing_token is not None and existing_request is not None:
            self._supersede_request(existing_token, existing_request)
        counts_against_cap = (
            request_mode == ImageRequestMode.BACKGROUND_PREFETCH
            and not background_cap_exempt
        )
        if (
            counts_against_cap
            and self._background_prefetch_in_flight
            >= _MAX_CONCURRENT_BACKGROUND_PREFETCH
        ):
            # Defer this neighbor's original-size fetch; prefetch_identify_position
            # re-issues it on the next navigation step, so this is a soft skip,
            # not a failure, and keeps the media budget bounded for a wide radius.
            return False
        if warming_memory_cache:
            # The worker now owns validation of the disk rendition. Until it
            # succeeds, do not let stale state suppress fallback delivery or
            # downgrade a complete waterfall failure to a partial failure.
            state.loaded_size = None
            previous_loaded_size = None
        effective_mode = (
            ImageRequestMode.ORIGINAL_UPGRADE
            if previous_loaded_size is not None
            and request_mode != ImageRequestMode.EXPLICIT_RETRY
            else request_mode
        )

        self._next_request_token += 1
        request = _ImageRequest(
            token=self._next_request_token,
            generation=self._generation,
            observation_id=observation_id,
            observation_index=observation_index,
            photo_id=photo.photo_id,
            photo_index=photo_index,
            request_mode=effective_mode,
            previous_loaded_size=previous_loaded_size,
            counts_against_background_cap=counts_against_cap,
            focus_exempt=background_cap_exempt,
            started_at=(time.monotonic() if log.isEnabledFor(logging.DEBUG) else 0.0),
        )
        if counts_against_cap:
            self._background_prefetch_in_flight += 1
        self._in_flight[request.token] = request
        self._request_for_photo[photo.photo_id] = request.token
        worker = _ImageWorker(
            request=request,
            candidates=candidates,
            client=self._client,
            disk_cache=self._disk_cache,
            is_cancelled=lambda generation=request.generation, token=request.token: (
                generation != self._generation or token in self._superseded_tokens
            ),
        )
        signals = worker.signals
        self._live_signals.add(signals)
        signals.terminal.connect(
            lambda result, source=signals: self._handle_terminal_result(source, result)
        )
        self._pool.start(worker, priority)
        if (
            log.isEnabledFor(logging.DEBUG)
            and effective_mode != ImageRequestMode.BACKGROUND_PREFETCH
        ):
            log.debug(
                "Image request started obs=%d index=%d photo=%d photo_index=%d "
                "mode=%s priority=%d candidates=%s",
                observation_id,
                observation_index,
                photo.photo_id,
                photo_index,
                effective_mode.value,
                priority,
                [size for size, _url in candidates],
            )
        self._emit_request_state()
        return True

    def _set_focused_photo(self, photo_id: int | None) -> None:
        """Retire the previous photo's uncapped request on focus changes."""
        previous_photo_id = self._focused_photo_id
        if previous_photo_id == photo_id:
            return
        self._focused_photo_id = photo_id
        if previous_photo_id is None:
            return
        token = self._request_for_photo.get(previous_photo_id)
        request = self._in_flight.get(token) if token is not None else None
        if token is None or request is None or not request.focus_exempt:
            return
        self._supersede_request(token, request)
        self._emit_request_state()

    def _supersede_request(self, token: int, request: _ImageRequest) -> None:
        """Cancel one logical request and ignore its eventual terminal result."""
        self._superseded_tokens.add(token)
        self._in_flight.pop(token, None)
        if self._request_for_photo.get(request.photo_id) == token:
            self._request_for_photo.pop(request.photo_id, None)
        if request.counts_against_background_cap:
            self._background_prefetch_in_flight = max(
                0,
                self._background_prefetch_in_flight - 1,
            )

    def _eligible_candidates(
        self,
        photo: StudyPhoto,
        state: _PhotoLoadState,
        request_mode: ImageRequestMode,
    ) -> tuple[tuple[tuple[str, str], ...], str | None]:
        now = time.monotonic()
        candidates = tuple(photo.candidate_size_urls())
        cached = self.get_best_memory_cached(photo.photo_id)
        cached_size = cached[1] if cached is not None else None
        if cached_size is not None:
            state.loaded_size = _better_size(state.loaded_size, cached_size)
        elif state.loaded_size is not None and not self._disk_cache.has(
            photo.photo_id, state.loaded_size
        ):
            state.loaded_size = None

        # A known disk rendition that has fallen out of the memory LRU should
        # be decoded by a worker, never synchronously while a navigation tick
        # is running. Start at that known size to avoid an early network
        # upgrade, but retain lower-quality fallbacks in case the disk entry is
        # missing or corrupt and its corresponding download also fails. Return
        # the known size as a warm-up marker; _start_request clears it only
        # after the worker has actually been admitted to the pool.
        if cached_size is None and state.loaded_size is not None:
            warm_candidates = _same_or_lower_quality_candidates(
                candidates,
                state.loaded_size,
            )
            if warm_candidates:
                return warm_candidates, state.loaded_size

        if state.loaded_size == "original":
            _clear_negative_state(state)
            return (), "original"

        if state.loaded_size is not None:
            upgrades = _higher_quality_candidates(candidates, state.loaded_size)
            if not upgrades:
                return (), state.loaded_size
            if request_mode == ImageRequestMode.EXPLICIT_RETRY:
                return upgrades, state.loaded_size
            if now < state.next_original_upgrade_at:
                return (), state.loaded_size
            return upgrades, state.loaded_size

        if (
            state.complete_failure is not None
            and request_mode != ImageRequestMode.EXPLICIT_RETRY
            and now < state.next_automatic_retry_at
        ):
            return (), None
        return candidates, None

    def _handle_terminal_result(
        self,
        signals: _ImageWorkerSignals,
        result: _ImageTerminalResult,
    ) -> None:
        self._live_signals.discard(signals)
        request = result.request
        self._superseded_tokens.discard(request.token)
        if request.generation != self._generation:
            return
        if self._in_flight.get(request.token) != request:
            return

        self._in_flight.pop(request.token, None)
        if self._request_for_photo.get(request.photo_id) == request.token:
            self._request_for_photo.pop(request.photo_id, None)
        if request.counts_against_background_cap:
            self._background_prefetch_in_flight = max(
                0, self._background_prefetch_in_flight - 1
            )
        if result.cancelled:
            self._emit_request_state()
            if (
                log.isEnabledFor(logging.DEBUG)
                and request.request_mode != ImageRequestMode.BACKGROUND_PREFETCH
            ):
                log.debug(
                    "Image request cancelled obs=%d index=%d photo=%d "
                    "mode=%s elapsed=%.1fms",
                    request.observation_id,
                    request.observation_index,
                    request.photo_id,
                    request.request_mode.value,
                    max(
                        0.0,
                        (time.monotonic() - request.started_at) * 1000.0,
                    ),
                )
            return

        state = self._state_for(request.photo_id, create=True)
        assert state is not None
        now = time.monotonic()
        if (
            result.image is not None
            and not result.image.isNull()
            and result.loaded_size
        ):
            self._accept_success(request, result, state, now)
        else:
            self._accept_failure(request, result.attempts, state, now)
        self._emit_request_state()
        if log.isEnabledFor(logging.DEBUG) and (
            request.request_mode != ImageRequestMode.BACKGROUND_PREFETCH
            or (request.photo_index == 0 and result.source == "network")
            or result.attempts
        ):
            log.debug(
                "Image request finished obs=%d index=%d photo=%d mode=%s source=%s "
                "size=%s elapsed=%.1fms attempts=%d",
                request.observation_id,
                request.observation_index,
                request.photo_id,
                request.request_mode.value,
                result.source,
                result.loaded_size,
                max(0.0, (now - request.started_at) * 1000.0),
                len(result.attempts),
            )

    def _accept_success(
        self,
        request: _ImageRequest,
        result: _ImageTerminalResult,
        state: _PhotoLoadState,
        now: float,
    ) -> None:
        assert result.image is not None and result.loaded_size is not None
        pixmap = QPixmap.fromImage(result.image)
        self._store_memory(request.photo_id, result.loaded_size, pixmap)
        state.loaded_size = _better_size(state.loaded_size, result.loaded_size)
        if result.loaded_size == "original":
            _clear_negative_state(state)
        elif result.attempts:
            _append_partial_failures(state, result.attempts)
            state.complete_failure = None
            state.last_failure_time = now
            state.next_original_upgrade_at = now + _ORIGINAL_UPGRADE_COOLDOWN_SECONDS
        self.image_ready.emit(
            request.observation_index,
            request.photo_index,
            request.observation_id,
            request.photo_id,
            pixmap,
        )
        self.image_ready_detailed.emit(
            request.observation_index,
            request.photo_index,
            request.observation_id,
            request.photo_id,
            result.loaded_size,
            pixmap,
        )
        self.image_diagnostics_changed.emit(request.photo_id)

    def _accept_failure(
        self,
        request: _ImageRequest,
        attempts: tuple[ImageAttemptFailure, ...],
        state: _PhotoLoadState,
        now: float,
    ) -> None:
        if request.previous_loaded_size is not None or state.loaded_size is not None:
            _append_partial_failures(state, attempts)
            state.complete_failure = None
            state.last_failure_time = now
            state.next_original_upgrade_at = now + _ORIGINAL_UPGRADE_COOLDOWN_SECONDS
            self.image_diagnostics_changed.emit(request.photo_id)
            return

        failure = ImageFailure(request.photo_id, attempts)
        state.complete_failure = failure
        state.last_failure_time = now
        state.next_automatic_retry_at = now + _TOTAL_FAILURE_COOLDOWN_SECONDS
        self.image_failed.emit(request.photo_id, "all", failure.message)
        self.image_failed_detailed.emit(failure)
        self.image_diagnostics_changed.emit(request.photo_id)

    def _state_for(self, photo_id: int, *, create: bool) -> _PhotoLoadState | None:
        state = self._photo_states.get(photo_id)
        if state is None and create:
            state = _PhotoLoadState()
            self._photo_states[photo_id] = state
        if state is not None:
            self._photo_states.move_to_end(photo_id)
        self._evict_photo_states()
        return state

    def _evict_photo_states(self) -> None:
        blocked = 0
        while len(self._photo_states) > _MAX_PHOTO_STATES and blocked < len(
            self._photo_states
        ):
            photo_id, _state = next(iter(self._photo_states.items()))
            if photo_id in self._request_for_photo:
                self._photo_states.move_to_end(photo_id)
                blocked += 1
                continue
            self._photo_states.popitem(last=False)
            blocked = 0

    def _emit_request_state(self) -> None:
        self.request_state_changed.emit(
            len(self._in_flight),
            tuple(sorted(self._request_for_photo)),
        )

    def _store_memory(self, photo_id: int, size: str, pixmap: QPixmap) -> None:
        key = (photo_id, size)
        previous = self._memory.get(key)
        if previous is not None:
            self._memory_bytes -= _pixmap_bytes(previous)
        self._memory[key] = pixmap
        self._memory.move_to_end(key)
        self._memory_bytes += _pixmap_bytes(pixmap)
        self._evict_memory_if_needed()

    def _evict_memory_if_needed(self) -> None:
        while self._memory_bytes > self._max_memory_bytes and self._memory:
            _, pixmap = self._memory.popitem(last=False)
            self._memory_bytes -= _pixmap_bytes(pixmap)
        self._memory_bytes = max(0, self._memory_bytes)


def _higher_quality_candidates(
    candidates: tuple[tuple[str, str], ...],
    loaded_size: str,
) -> tuple[tuple[str, str], ...]:
    try:
        loaded_rank = _SIZE_ORDER.index(loaded_size)
    except ValueError:
        return candidates
    return tuple(
        (size, url)
        for size, url in candidates
        if size in _SIZE_ORDER and _SIZE_ORDER.index(size) < loaded_rank
    )


def _same_or_lower_quality_candidates(
    candidates: tuple[tuple[str, str], ...],
    loaded_size: str,
) -> tuple[tuple[str, str], ...]:
    try:
        loaded_rank = _SIZE_ORDER.index(loaded_size)
    except ValueError:
        return candidates
    return tuple(
        (size, url)
        for size, url in candidates
        if size in _SIZE_ORDER and _SIZE_ORDER.index(size) >= loaded_rank
    )


def _better_size(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    try:
        return (
            candidate
            if _SIZE_ORDER.index(candidate) < _SIZE_ORDER.index(current)
            else current
        )
    except ValueError:
        return candidate


def _clear_negative_state(state: _PhotoLoadState) -> None:
    state.partial_failures.clear()
    state.complete_failure = None
    state.next_automatic_retry_at = 0.0
    state.next_original_upgrade_at = 0.0
    state.last_failure_time = 0.0


def _append_partial_failures(
    state: _PhotoLoadState,
    attempts: tuple[ImageAttemptFailure, ...],
) -> None:
    state.partial_failures.extend(attempts)
    if len(state.partial_failures) > _MAX_PARTIAL_FAILURES:
        del state.partial_failures[:-_MAX_PARTIAL_FAILURES]


def _decode_image(data: bytes) -> QImage:
    image = QImage()
    image.loadFromData(data)
    return image


def _detect_extension(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def _failure_from_exception(
    photo_id: int,
    url: str,
    size: str,
    candidate_number: int,
    exc: Exception,
) -> ImageAttemptFailure:
    return ImageAttemptFailure(
        photo_id=photo_id,
        url=url,
        size=size,
        candidate_number=candidate_number,
        exception_type=type(exc).__name__,
        message=str(exc),
        status_code=_exception_status_code(exc),
        timestamp=_utc_timestamp(),
    )


def _exception_status_code(exc: Exception) -> int | None:
    if isinstance(exc, FileNotFoundError):
        return 404
    direct_status = getattr(exc, "status_code", None)
    if isinstance(direct_status, int):
        return direct_status
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _pixmap_bytes(pixmap: QPixmap) -> int:
    return max(0, pixmap.width() * pixmap.height() * 4)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
