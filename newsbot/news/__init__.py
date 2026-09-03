"""News sources: GNews API first, Google News RSS as the free fallback."""

from .fetcher import NewsFetcher
from .models import Article, article_key, clean_url

__all__ = ["NewsFetcher", "Article", "article_key", "clean_url"]
