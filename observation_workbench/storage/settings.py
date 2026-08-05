"""
Persistent application settings using QSettings (INI format).
Provides typed accessors for all user-configurable values.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QSettings, QByteArray

log = logging.getLogger(__name__)

# On-disk QSettings identity (registry key / ~/.config path).  The app was
# previously called "iNat ID Study Viewer" and wrote under the LEGACY_* names
# below; _migrate_legacy_settings() copies that store forward on first run so
# an existing install keeps its saved token, filters, geometry and preferences.
APP_NAME = "ObservationWorkbench"
ORG_NAME = "ObservationWorkbench"

LEGACY_APP_NAME = "iNatStudyViewer"
LEGACY_ORG_NAME = "iNatStudy"

# Cache/journal directory.  Also renamed, also migrated — see
# _migrate_legacy_cache_dir().
DEFAULT_CACHE_DIR_NAME = "observation_workbench"
LEGACY_CACHE_DIR_NAME = "inat_study"

# Defaults
DEFAULT_CACHE_MAX_GB = 2.0
DEFAULT_MEMORY_CACHE_MAX_MB = 256
DEFAULT_PREFETCH_RADIUS = 2  # items either side of current
DEFAULT_SCROLL_SPEED_MULTIPLIER = 1.0
DEFAULT_NAVIGATION_REPEAT_RATE = 4.0  # observations per second


class AppSettings:
    def __init__(self) -> None:
        self._s = QSettings(ORG_NAME, APP_NAME)
        self._migrate_legacy_settings()
        self._restrict_file_permissions()
        self._migrate_legacy_cache_dir()

    def _migrate_legacy_settings(self) -> None:
        """Copy the pre-rename settings store forward, once.

        Runs only when the current store is empty, so a user who has already
        started using the new store is never overwritten by a stale legacy
        file.  The legacy store is left in place rather than deleted: it costs
        a few kilobytes and makes downgrading painless.
        """
        if self._s.allKeys():
            return
        legacy = QSettings(LEGACY_ORG_NAME, LEGACY_APP_NAME)
        keys = legacy.allKeys()
        if not keys:
            return
        for key in keys:
            self._s.setValue(key, legacy.value(key))
        self._s.sync()
        if self._s.status() != QSettings.Status.NoError:
            log.warning(
                "Could not fully migrate legacy settings from %s (status=%s)",
                legacy.fileName(), self._s.status(),
            )
            return
        log.info("Migrated %d settings from %s to %s",
                 len(keys), legacy.fileName(), self._s.fileName())

    def _migrate_legacy_cache_dir(self) -> None:
        """Move ~/.cache/inat_study to ~/.cache/observation_workbench, once.

        Skipped entirely when the user has pinned an explicit cache directory,
        and when the destination already exists — two populated cache/journal
        directories are never merged, because the Identify journal and the
        SQLite metadata cache cannot be combined safely.
        """
        if self._s.contains("cache/dir"):
            return
        legacy = Path.home() / ".cache" / LEGACY_CACHE_DIR_NAME
        current = Path.home() / ".cache" / DEFAULT_CACHE_DIR_NAME
        if not legacy.is_dir():
            return
        if current.exists():
            log.warning(
                "Both the legacy cache directory (%s) and the current one (%s) exist; "
                "using the current one and leaving the legacy data in place. "
                "Merge or delete it by hand if you want the old cache back.",
                legacy, current,
            )
            return
        try:
            current.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(current)
        except OSError as exc:
            # Cross-device or permission failure: keep using the old location
            # rather than silently starting from an empty cache and journal.
            log.warning("Could not migrate cache directory %s -> %s: %s", legacy, current, exc)
            self._s.setValue("cache/dir", str(legacy))
            self._s.sync()
            return
        log.info("Migrated cache directory %s -> %s", legacy, current)

    def _restrict_file_permissions(self) -> None:
        """Best-effort POSIX hardening for the file containing API credentials."""
        if os.name != "posix":
            return
        path = Path(self._s.fileName())
        if not path.is_file():
            return
        try:
            os.chmod(path, 0o600)
        except OSError:
            log.warning("Could not restrict settings file permissions: %s", path)

    # ------------------------------------------------------------------
    # Last-used filter values
    # ------------------------------------------------------------------

    @property
    def last_username(self) -> str:
        return self._s.value("filter/username", "", type=str)

    @last_username.setter
    def last_username(self, v: str) -> None:
        self._s.setValue("filter/username", v)

    @property
    def last_place_id(self) -> Optional[int]:
        v = self._s.value("filter/place_id", None)
        return int(v) if v not in (None, "") else None

    @last_place_id.setter
    def last_place_id(self, v: Optional[int]) -> None:
        self._s.setValue("filter/place_id", v)

    @property
    def last_place_name(self) -> str:
        return self._s.value("filter/place_name", "", type=str)

    @last_place_name.setter
    def last_place_name(self, v: str) -> None:
        self._s.setValue("filter/place_name", v)

    @property
    def last_taxon_id(self) -> Optional[int]:
        v = self._s.value("filter/taxon_id", None)
        return int(v) if v not in (None, "") else None

    @last_taxon_id.setter
    def last_taxon_id(self, v: Optional[int]) -> None:
        self._s.setValue("filter/taxon_id", v)

    @property
    def last_taxon_name(self) -> str:
        return self._s.value("filter/taxon_name", "", type=str)

    @last_taxon_name.setter
    def last_taxon_name(self, v: str) -> None:
        self._s.setValue("filter/taxon_name", v)

    @property
    def username_history(self) -> List[str]:
        v = self._s.value("filter/username_history", "[]", type=str)
        try:
            result = json.loads(v)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    @username_history.setter
    def username_history(self, v: List[str]) -> None:
        self._s.setValue("filter/username_history", json.dumps(v))

    @property
    def rank_filter_enabled(self) -> bool:
        return self._s.value("filter/rank_enabled", True, type=bool)

    @rank_filter_enabled.setter
    def rank_filter_enabled(self, v: bool) -> None:
        self._s.setValue("filter/rank_enabled", v)

    @property
    def rank_filter_name(self) -> str:
        return str(self._s.value("filter/rank_name", "Species"))

    @rank_filter_name.setter
    def rank_filter_name(self, v: str) -> None:
        self._s.setValue("filter/rank_name", v)

    @property
    def rank_exact(self) -> bool:
        return self._s.value("filter/rank_exact", False, type=bool)

    @rank_exact.setter
    def rank_exact(self, v: bool) -> None:
        self._s.setValue("filter/rank_exact", v)

    @property
    def provisional_name_only(self) -> bool:
        return self._s.value("filter/provisional_name_only", False, type=bool)

    @provisional_name_only.setter
    def provisional_name_only(self, v: bool) -> None:
        self._s.setValue("filter/provisional_name_only", v)

    # ------------------------------------------------------------------
    # Bulk agree-to-provisional setup defaults
    # ------------------------------------------------------------------

    @property
    def bulk_agree_url(self) -> str:
        return self._s.value("bulk_agree/url", "", type=str)

    @bulk_agree_url.setter
    def bulk_agree_url(self, v: str) -> None:
        self._s.setValue("bulk_agree/url", v.strip())

    @property
    def bulk_agree_source_mode(self) -> str:
        """Either "url" or "current" (the query already loaded in the viewer)."""
        value = self._s.value("bulk_agree/source_mode", "url", type=str)
        return value if value in ("url", "current") else "url"

    @bulk_agree_source_mode.setter
    def bulk_agree_source_mode(self, v: str) -> None:
        self._s.setValue("bulk_agree/source_mode", v if v in ("url", "current") else "url")

    @property
    def bulk_agree_require_dna_barcode_its(self) -> bool:
        return self._s.value("bulk_agree/require_dna_barcode_its", True, type=bool)

    @bulk_agree_require_dna_barcode_its.setter
    def bulk_agree_require_dna_barcode_its(self, v: bool) -> None:
        self._s.setValue("bulk_agree/require_dna_barcode_its", v)

    @property
    def bulk_agree_only_if_needed(self) -> bool:
        return self._s.value("bulk_agree/only_if_needed", True, type=bool)

    @bulk_agree_only_if_needed.setter
    def bulk_agree_only_if_needed(self, v: bool) -> None:
        self._s.setValue("bulk_agree/only_if_needed", v)

    @property
    def bulk_agree_max_observations(self) -> int:
        return int(self._s.value("bulk_agree/max_observations", 100))

    @bulk_agree_max_observations.setter
    def bulk_agree_max_observations(self, v: int) -> None:
        self._s.setValue("bulk_agree/max_observations", int(v))

    @property
    def bulk_agree_delay_min_seconds(self) -> int:
        return int(self._s.value("bulk_agree/delay_min_seconds", 10))

    @bulk_agree_delay_min_seconds.setter
    def bulk_agree_delay_min_seconds(self, v: int) -> None:
        self._s.setValue("bulk_agree/delay_min_seconds", int(v))

    @property
    def bulk_agree_delay_max_seconds(self) -> int:
        return int(self._s.value("bulk_agree/delay_max_seconds", 30))

    @bulk_agree_delay_max_seconds.setter
    def bulk_agree_delay_max_seconds(self, v: int) -> None:
        self._s.setValue("bulk_agree/delay_max_seconds", int(v))

    @property
    def bulk_agree_dry_run(self) -> bool:
        return self._s.value("bulk_agree/dry_run", False, type=bool)

    @bulk_agree_dry_run.setter
    def bulk_agree_dry_run(self, v: bool) -> None:
        self._s.setValue("bulk_agree/dry_run", v)

    # ------------------------------------------------------------------
    # Bulk disagree setup defaults
    # ------------------------------------------------------------------

    @property
    def bulk_disagree_url(self) -> str:
        return self._s.value("bulk_disagree/url", "", type=str)

    @bulk_disagree_url.setter
    def bulk_disagree_url(self, v: str) -> None:
        self._s.setValue("bulk_disagree/url", v.strip())

    @property
    def bulk_disagree_target_taxon_id(self) -> Optional[int]:
        v = self._s.value("bulk_disagree/target_taxon_id", None)
        return int(v) if v not in (None, "") else None

    @bulk_disagree_target_taxon_id.setter
    def bulk_disagree_target_taxon_id(self, v: Optional[int]) -> None:
        self._s.setValue("bulk_disagree/target_taxon_id", v)

    @property
    def bulk_disagree_target_taxon_name(self) -> str:
        return self._s.value("bulk_disagree/target_taxon_name", "", type=str)

    @bulk_disagree_target_taxon_name.setter
    def bulk_disagree_target_taxon_name(self, v: str) -> None:
        self._s.setValue("bulk_disagree/target_taxon_name", v.strip())

    @property
    def bulk_disagree_target_taxon_rank(self) -> str:
        return self._s.value("bulk_disagree/target_taxon_rank", "", type=str)

    @bulk_disagree_target_taxon_rank.setter
    def bulk_disagree_target_taxon_rank(self, v: str) -> None:
        self._s.setValue("bulk_disagree/target_taxon_rank", v.strip())

    @property
    def bulk_disagree_comment(self) -> str:
        return self._s.value("bulk_disagree/comment", "", type=str)

    @bulk_disagree_comment.setter
    def bulk_disagree_comment(self, v: str) -> None:
        self._s.setValue("bulk_disagree/comment", v)

    @property
    def bulk_disagree_skip_dna_barcode_its(self) -> bool:
        return self._s.value("bulk_disagree/skip_dna_barcode_its", True, type=bool)

    @bulk_disagree_skip_dna_barcode_its.setter
    def bulk_disagree_skip_dna_barcode_its(self, v: bool) -> None:
        self._s.setValue("bulk_disagree/skip_dna_barcode_its", v)

    @property
    def bulk_disagree_only_dna_barcode_its(self) -> bool:
        return self._s.value("bulk_disagree/only_dna_barcode_its", False, type=bool)

    @bulk_disagree_only_dna_barcode_its.setter
    def bulk_disagree_only_dna_barcode_its(self, v: bool) -> None:
        self._s.setValue("bulk_disagree/only_dna_barcode_its", v)

    @property
    def bulk_disagree_dqa_vote_requested(self) -> bool:
        return self._s.value("bulk_disagree/dqa_vote_requested", False, type=bool)

    @bulk_disagree_dqa_vote_requested.setter
    def bulk_disagree_dqa_vote_requested(self, v: bool) -> None:
        self._s.setValue("bulk_disagree/dqa_vote_requested", v)

    @property
    def bulk_disagree_require_source_taxon_match(self) -> bool:
        return self._s.value("bulk_disagree/require_source_taxon_match", True, type=bool)

    @bulk_disagree_require_source_taxon_match.setter
    def bulk_disagree_require_source_taxon_match(self, v: bool) -> None:
        self._s.setValue("bulk_disagree/require_source_taxon_match", v)

    @property
    def bulk_disagree_max_observations(self) -> int:
        return int(self._s.value("bulk_disagree/max_observations", 100))

    @bulk_disagree_max_observations.setter
    def bulk_disagree_max_observations(self, v: int) -> None:
        self._s.setValue("bulk_disagree/max_observations", int(v))

    @property
    def bulk_disagree_delay_min_seconds(self) -> int:
        return int(self._s.value("bulk_disagree/delay_min_seconds", 10))

    @bulk_disagree_delay_min_seconds.setter
    def bulk_disagree_delay_min_seconds(self, v: int) -> None:
        self._s.setValue("bulk_disagree/delay_min_seconds", int(v))

    @property
    def bulk_disagree_delay_max_seconds(self) -> int:
        return int(self._s.value("bulk_disagree/delay_max_seconds", 30))

    @bulk_disagree_delay_max_seconds.setter
    def bulk_disagree_delay_max_seconds(self, v: int) -> None:
        self._s.setValue("bulk_disagree/delay_max_seconds", int(v))

    @property
    def bulk_disagree_dry_run(self) -> bool:
        return self._s.value("bulk_disagree/dry_run", False, type=bool)

    @bulk_disagree_dry_run.setter
    def bulk_disagree_dry_run(self, v: bool) -> None:
        self._s.setValue("bulk_disagree/dry_run", v)

    # ------------------------------------------------------------------
    # Propose-name (to observation numbers) setup defaults
    # ------------------------------------------------------------------

    @property
    def propose_name_target_taxon_id(self) -> Optional[int]:
        v = self._s.value("propose_name/target_taxon_id", None)
        return int(v) if v not in (None, "") else None

    @propose_name_target_taxon_id.setter
    def propose_name_target_taxon_id(self, v: Optional[int]) -> None:
        self._s.setValue("propose_name/target_taxon_id", v)

    @property
    def propose_name_target_taxon_name(self) -> str:
        return self._s.value("propose_name/target_taxon_name", "", type=str)

    @propose_name_target_taxon_name.setter
    def propose_name_target_taxon_name(self, v: str) -> None:
        self._s.setValue("propose_name/target_taxon_name", v.strip())

    @property
    def propose_name_target_taxon_rank(self) -> str:
        return self._s.value("propose_name/target_taxon_rank", "", type=str)

    @propose_name_target_taxon_rank.setter
    def propose_name_target_taxon_rank(self, v: str) -> None:
        self._s.setValue("propose_name/target_taxon_rank", v.strip())

    @property
    def propose_name_comment(self) -> str:
        return self._s.value("propose_name/comment", "", type=str)

    @propose_name_comment.setter
    def propose_name_comment(self, v: str) -> None:
        self._s.setValue("propose_name/comment", v)

    @property
    def propose_name_delay_min_seconds(self) -> int:
        return int(self._s.value("propose_name/delay_min_seconds", 10))

    @propose_name_delay_min_seconds.setter
    def propose_name_delay_min_seconds(self, v: int) -> None:
        self._s.setValue("propose_name/delay_min_seconds", int(v))

    @property
    def propose_name_delay_max_seconds(self) -> int:
        return int(self._s.value("propose_name/delay_max_seconds", 30))

    @propose_name_delay_max_seconds.setter
    def propose_name_delay_max_seconds(self, v: int) -> None:
        self._s.setValue("propose_name/delay_max_seconds", int(v))

    @property
    def propose_name_dry_run(self) -> bool:
        return self._s.value("propose_name/dry_run", False, type=bool)

    @propose_name_dry_run.setter
    def propose_name_dry_run(self, v: bool) -> None:
        self._s.setValue("propose_name/dry_run", v)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @property
    def inat_api_token(self) -> str:
        return self._s.value("auth/inat_api_token", "", type=str)

    @inat_api_token.setter
    def inat_api_token(self, v: str) -> None:
        self._s.setValue("auth/inat_api_token", v.strip())
        self.sync()

    @property
    def inat_login(self) -> str:
        return self._s.value("auth/inat_login", "", type=str)

    @inat_login.setter
    def inat_login(self, v: str) -> None:
        self._s.setValue("auth/inat_login", v.strip())

    def clear_auth(self) -> None:
        self._s.remove("auth/inat_api_token")
        self._s.remove("auth/inat_login")
        self.sync()

    # Identify is intentionally separate from the study-viewer settings.
    @property
    def identify_last_url(self) -> str:
        return self._s.value("identify/last_url", "", type=str)

    @identify_last_url.setter
    def identify_last_url(self, value: str) -> None:
        self._s.setValue("identify/last_url", value.strip())

    @property
    def identify_session_limit(self) -> int:
        return max(1, int(self._s.value("identify/session_limit", 200)))

    @identify_session_limit.setter
    def identify_session_limit(self, value: int) -> None:
        self._s.setValue("identify/session_limit", max(1, int(value)))

    @property
    def identify_prefetch_radius(self) -> int:
        return max(0, int(self._s.value("identify/prefetch_radius", 3)))

    @identify_prefetch_radius.setter
    def identify_prefetch_radius(self, value: int) -> None:
        self._s.setValue("identify/prefetch_radius", max(0, int(value)))

    @property
    def identify_resume_actions_automatically(self) -> bool:
        """Reserved safety preference; automatic action resume stays disabled."""
        return self._s.value("identify/resume_actions_automatically", False, type=bool)

    @identify_resume_actions_automatically.setter
    def identify_resume_actions_automatically(self, value: bool) -> None:
        # Retain a persisted preference for forward compatibility, but this gate
        # never reads it to resume writes automatically.
        self._s.setValue("identify/resume_actions_automatically", bool(value))

    @property
    def identify_advance_after_identification(self) -> bool:
        """Advance one Identify item after a newly journaled identification action."""
        return self._s.value("identify/advance_after_identification", True, type=bool)

    @identify_advance_after_identification.setter
    def identify_advance_after_identification(self, value: bool) -> None:
        self._s.setValue("identify/advance_after_identification", bool(value))

    @property
    def identify_window_geometry(self):
        return self._s.value("identify/geometry")

    @identify_window_geometry.setter
    def identify_window_geometry(self, value) -> None:
        self._s.setValue("identify/geometry", value)

    @property
    def identify_window_state(self):
        return self._s.value("identify/state")

    @identify_window_state.setter
    def identify_window_state(self, value) -> None:
        self._s.setValue("identify/state", value)

    @property
    def identify_splitter_state(self):
        return self._s.value("identify/splitter_state")

    @identify_splitter_state.setter
    def identify_splitter_state(self, value) -> None:
        self._s.setValue("identify/splitter_state", value)

    @property
    def identify_active_tab(self) -> int:
        return int(self._s.value("identify/active_tab", 0))

    @identify_active_tab.setter
    def identify_active_tab(self, value: int) -> None:
        self._s.setValue("identify/active_tab", int(value))

    @property
    def identify_show_reviewed(self) -> bool:
        """Whether the Identify queue includes observations already reviewed by the viewer."""
        return self._s.value("identify/show_reviewed", False, type=bool)

    @identify_show_reviewed.setter
    def identify_show_reviewed(self, value: bool) -> None:
        self._s.setValue("identify/show_reviewed", bool(value))

    # ------------------------------------------------------------------
    # Window geometry
    # ------------------------------------------------------------------

    @property
    def window_geometry(self) -> Optional[QByteArray]:
        return self._s.value("window/geometry")

    @window_geometry.setter
    def window_geometry(self, v: QByteArray) -> None:
        self._s.setValue("window/geometry", v)

    @property
    def window_state(self) -> Optional[QByteArray]:
        return self._s.value("window/state")

    @window_state.setter
    def window_state(self, v: QByteArray) -> None:
        self._s.setValue("window/state", v)

    @property
    def splitter_state(self) -> Optional[QByteArray]:
        return self._s.value("window/splitter_state")

    @splitter_state.setter
    def splitter_state(self, v: QByteArray) -> None:
        self._s.setValue("window/splitter_state", v)

    # ------------------------------------------------------------------
    # Cache settings
    # ------------------------------------------------------------------

    @property
    def cache_dir(self) -> Path:
        default = str(Path.home() / ".cache" / DEFAULT_CACHE_DIR_NAME)
        v = self._s.value("cache/dir", default, type=str)
        return Path(v)

    @cache_dir.setter
    def cache_dir(self, v: Path) -> None:
        self._s.setValue("cache/dir", str(v))

    @property
    def cache_max_gb(self) -> float:
        return float(self._s.value("cache/max_gb", DEFAULT_CACHE_MAX_GB))

    @cache_max_gb.setter
    def cache_max_gb(self, v: float) -> None:
        self._s.setValue("cache/max_gb", v)

    @property
    def memory_cache_max_mb(self) -> int:
        return int(self._s.value("cache/memory_max_mb", DEFAULT_MEMORY_CACHE_MAX_MB))

    @memory_cache_max_mb.setter
    def memory_cache_max_mb(self, v: int) -> None:
        self._s.setValue("cache/memory_max_mb", v)

    @property
    def prefetch_radius(self) -> int:
        return int(self._s.value("cache/prefetch_radius", DEFAULT_PREFETCH_RADIUS))

    @prefetch_radius.setter
    def prefetch_radius(self, v: int) -> None:
        self._s.setValue("cache/prefetch_radius", v)

    @property
    def navigation_repeat_rate(self) -> float:
        return max(
            0.25,
            float(
                self._s.value(
                    "navigation/repeat_rate",
                    DEFAULT_NAVIGATION_REPEAT_RATE,
                )
            ),
        )

    @navigation_repeat_rate.setter
    def navigation_repeat_rate(self, v: float) -> None:
        self._s.setValue("navigation/repeat_rate", max(0.25, float(v)))

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    @property
    def ui_font_scale(self) -> float:
        return float(self._s.value("display/font_scale", 1.0))

    @ui_font_scale.setter
    def ui_font_scale(self, v: float) -> None:
        self._s.setValue("display/font_scale", v)

    @property
    def show_common_names(self) -> bool:
        return self._s.value("display/show_common_names", False, type=bool)

    @show_common_names.setter
    def show_common_names(self, v: bool) -> None:
        self._s.setValue("display/show_common_names", v)

    @property
    def result_list_font_scale(self) -> float:
        return float(self._s.value("display/result_list_font_scale", 1.0))

    @result_list_font_scale.setter
    def result_list_font_scale(self, v: float) -> None:
        self._s.setValue("display/result_list_font_scale", v)

    @property
    def scroll_speed_multiplier(self) -> float:
        try:
            value = float(
                self._s.value(
                    "display/scroll_speed_multiplier",
                    DEFAULT_SCROLL_SPEED_MULTIPLIER,
                )
            )
        except (TypeError, ValueError):
            value = DEFAULT_SCROLL_SPEED_MULTIPLIER
        return max(0.25, min(10.0, value))

    @scroll_speed_multiplier.setter
    def scroll_speed_multiplier(self, v: float) -> None:
        try:
            value = float(v)
        except (TypeError, ValueError):
            value = DEFAULT_SCROLL_SPEED_MULTIPLIER
        self._s.setValue("display/scroll_speed_multiplier", max(0.25, min(10.0, value)))

    # Gate 1B Mushroom Observer credentials.  Callers must present the
    # plaintext-storage warning and obtain explicit opt-in before invoking the
    # store method.  Reconciliation otherwise keeps keys in memory only.
    def reconciliation_mo_api_key(self, mo_user_id: int) -> str:
        return self._s.value(
            f"reconciliation/mo_api_keys/{int(mo_user_id)}", "", type=str
        ).strip()

    def store_reconciliation_mo_api_key(self, mo_user_id: int, api_key: str) -> None:
        self._s.setValue(
            f"reconciliation/mo_api_keys/{int(mo_user_id)}", api_key.strip()
        )
        self.sync()

    def clear_reconciliation_mo_api_key(self, mo_user_id: int) -> None:
        self._s.remove(f"reconciliation/mo_api_keys/{int(mo_user_id)}")
        self.sync()

    # Last Mushroom Observer username typed into the reconciliation setup
    # dialog, so it can be prefilled next time. A username is not a credential
    # (unlike the API key above), so this needs no opt-in.
    @property
    def reconciliation_last_mo_login(self) -> str:
        return self._s.value("reconciliation/last_mo_login", "", type=str).strip()

    @reconciliation_last_mo_login.setter
    def reconciliation_last_mo_login(self, value: str) -> None:
        self._s.setValue("reconciliation/last_mo_login", str(value or "").strip())
        self.sync()

    def sync(self) -> None:
        self._s.sync()
        self._restrict_file_permissions()
