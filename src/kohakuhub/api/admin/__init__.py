"""Admin API - Requires admin secret token authentication.

Organized router structure:
- session: Browser sessions of the admin panel (the shared token exchanged for an HttpOnly cookie)
- repo_actions: Delete, move and squash a repository as the administrator (the panel's cookie only reaches /admin)
- users: User management (CRUD, verification)
- repositories: Repository management and file browsing
- quota: Quota management and recalculation
- stats: System statistics and analytics
- overview: One call for the admin Overview (status, attention, counts, activity)
- storage: S3 bucket and object browsing
- commits: Commit history
- invitations: Invitation management
- search: Global search across entities
- health: Live probes for backing services (Postgres, MinIO, LakeFS, SMTP)
- cache: L2 cache (Valkey) hit/miss/error counters and memory state
- tasks: Background task queue inspection, retry, and discard
"""

from fastapi import APIRouter

from kohakuhub.api.admin.routers import (
    auth_router,
    cache_router,
    commits_router,
    credentials_router,
    database_router,
    fallback_router,
    health_router,
    invitations_router,
    overview_router,
    quota_router,
    repo_actions_router,
    repositories_router,
    search_router,
    session_router,
    site_appearance_router,
    site_branding_router,
    site_homepage_router,
    stats_router,
    storage_router,
    tasks_router,
    users_router,
)

# Create main admin router
router = APIRouter()

# Include all sub-routers
router.include_router(auth_router, tags=["admin-auth"])
router.include_router(session_router, tags=["admin-session"])
router.include_router(repo_actions_router, tags=["admin-repo-actions"])
router.include_router(users_router, tags=["admin-users"])
router.include_router(repositories_router, tags=["admin-repositories"])
router.include_router(quota_router, tags=["admin-quota"])
router.include_router(stats_router, tags=["admin-stats"])
router.include_router(overview_router, tags=["admin-overview"])
router.include_router(storage_router, tags=["admin-storage"])
router.include_router(commits_router, tags=["admin-commits"])
router.include_router(invitations_router, tags=["admin-invitations"])
router.include_router(search_router, tags=["admin-search"])
router.include_router(database_router, tags=["admin-database"])
router.include_router(fallback_router, tags=["admin-fallback"])
router.include_router(health_router, tags=["admin-health"])
router.include_router(credentials_router, tags=["admin-credentials"])
router.include_router(cache_router, tags=["admin-cache"])
router.include_router(tasks_router, tags=["admin-tasks"])
router.include_router(site_branding_router, tags=["admin-site-branding"])
router.include_router(site_appearance_router, tags=["admin-site-appearance"])
router.include_router(site_homepage_router, tags=["admin-site-homepage"])

__all__ = ["router"]
