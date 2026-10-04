"""Trending score calculation utilities."""

from datetime import date, datetime, timedelta, timezone

from kohakuhub.config import cfg
from kohakuhub.db import DailyRepoStats, Repository
from kohakuhub.logger import get_logger

logger = get_logger("TRENDING")


def calculate_trending_scores(repo_type: str, days: int = 7) -> dict[int, float]:
    """Calculate trending scores for all repositories of a type.

    Trending algorithm:
    - Uses last N days of download/like data
    - Applies exponential decay (recent days weighted more)
    - Uses logarithmic scaling to help new repos compete
    - Formula: sum over days of: log(1 + daily_activity) * decay^days_ago

    Args:
        repo_type: Repository type to calculate for
        days: Number of days to consider (default: 7)

    Returns:
        Dict mapping repository_id -> trending_score
    """
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days - 1)

    # Decay factor (0.8 means yesterday is 80% as valuable as today)
    decay_factor = 0.8

    # Query all daily stats for the period
    # Need to join with Repository to filter by repo_type
    stats = list(
        DailyRepoStats.select(
            DailyRepoStats.repository,
            DailyRepoStats.date,
            DailyRepoStats.download_sessions,
        )
        .join(Repository)
        .where(
            (DailyRepoStats.date >= start_date)
            & (DailyRepoStats.date <= today)
            & (Repository.repo_type == repo_type)
        )
    )

    # Calculate trending scores
    import math

    trending_scores = {}  # repo_id -> score

    for stat in stats:
        # Use the loaded scalar FK instead of dereferencing the related model,
        # which otherwise performs one lazy query per stats row.
        repo_id = stat.repository_id
        days_ago = (today - stat.date).days

        # Decay weight: 1.0 for today, 0.8 for yesterday, 0.64 for 2 days ago, etc.
        decay = math.pow(decay_factor, days_ago)

        # Activity score: log(1 + downloads) to help new repos
        # log(1 + x) ensures: 0 downloads = 0, 10 downloads ≈ 2.4, 100 downloads ≈ 4.6
        activity = math.log(1 + stat.download_sessions)

        # Daily contribution to trend score
        daily_score = activity * decay

        if repo_id not in trending_scores:
            trending_scores[repo_id] = 0.0

        trending_scores[repo_id] += daily_score

    return trending_scores


def get_trending_repositories(
    repo_type: str, limit: int = 20, days: int = 7, scope=None
) -> list[Repository]:
    """Get trending repositories sorted by trending score.

    Args:
        repo_type: Repository type filter
        limit: Maximum number of repositories
        days: Number of days for trend calculation

    Returns:
        List of Repository objects sorted by trending score
    """
    # Calculate scores for all repos
    scores = calculate_trending_scores(repo_type, days)
    candidates = (scope if scope is not None else Repository.select()).where(
        (Repository.repo_type == repo_type) & (Repository.private == False)
    )

    if not scores:
        # No trending data, fall back to recent
        return list(candidates.order_by(Repository.created_at.desc()).limit(limit))

    # Sort by score and get top repos
    eligible_ids = set(
        candidates.select(Repository.id).where(Repository.id.in_(list(scores))).scalars()
    )
    sorted_repo_ids = sorted(
        ((repo_id, score) for repo_id, score in scores.items() if repo_id in eligible_ids),
        key=lambda x: x[1],
        reverse=True,
    )[:limit]

    # Fetch repositories in score order
    repos = []
    for repo_id, score in sorted_repo_ids:
        repo = Repository.get_or_none(Repository.id == repo_id)
        if repo and not repo.private:  # Only public repos in trending
            repos.append(repo)

    return repos
