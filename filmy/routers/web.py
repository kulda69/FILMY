"""Skladaci HTML router a kompatibilni vstupni bod pro web routy."""

from __future__ import annotations

from fastapi import APIRouter

from filmy.app_shared import (
    background_supervisor,
    launch_homepage_warmup,
    launch_person_portrait_warmup,
    launch_person_presentation_warmup,
    selected_panel_page,
    signal_metadata_pipeline,
)
from filmy.db import (
    delete_ai_recommendation_file,
    get_favorite_traits,
    get_hot_watchlist_page,
    get_latest_ai_recommendation_for_title,
    get_latest_genre_scores,
    get_local_library_status,
    get_person_presentation,
    get_recently_watched_page,
    get_title_people_panel,
    get_title_presentation,
    get_title_role_signals,
    get_user_list_items_page,
    get_watched_page,
    import_ai_recommendations_file,
    list_ai_recommendation_files,
)
from filmy.integrations.tmdb import get_tmdb_status

from .web_home import router as home_router
from .web_lists import router as lists_router
from .web_search import router as search_router
from .web_suggestions import router as suggestions_router
from .web_system import router as system_router
from .web_titles import router as titles_router

router = APIRouter()
router.include_router(home_router)
router.include_router(search_router)
router.include_router(lists_router)
router.include_router(suggestions_router)
router.include_router(system_router)
router.include_router(titles_router)
