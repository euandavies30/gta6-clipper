from ..config import Account, Settings
from .base import DryRunPoster, Poster, PostError, PostResult
from .instagram import InstagramPoster
from .tiktok import TikTokPoster
from .webhook import WebhookPoster

REGISTRY: dict[str, type[Poster]] = {
    "tiktok": TikTokPoster,
    "instagram": InstagramPoster,
    "webhook": WebhookPoster,
    "dryrun": DryRunPoster,
}


def get_poster(account: Account, settings: Settings) -> Poster:
    """DRY_RUN=true forces the dry-run poster for every account."""
    if settings.dry_run:
        return DryRunPoster(account, settings)
    try:
        return REGISTRY[account.poster](account, settings)
    except KeyError:
        raise ValueError(f"unknown poster '{account.poster}' for {account.name}; options: {sorted(REGISTRY)}")


__all__ = ["get_poster", "Poster", "PostError", "PostResult", "REGISTRY"]
