from .base import Adapter, IngestResult
from .website import WebsiteAdapter
from .meta_ads import MetaAdsAdapter
from .google_ads import GoogleAdsAdapter
from .amazon_brand_store import AmazonBrandStoreAdapter
from .ispot_tv import TvAdsAdapter

ADAPTER_REGISTRY: dict[str, type[Adapter]] = {
    "website": WebsiteAdapter,
    "meta_ads": MetaAdsAdapter,
    "google_ads": GoogleAdsAdapter,
    "amazon_brand_store": AmazonBrandStoreAdapter,
    "tv_ads": TvAdsAdapter,
}


def get_adapter(source_type: str) -> type[Adapter]:
    try:
        return ADAPTER_REGISTRY[source_type]
    except KeyError as e:
        raise ValueError(f"unknown source type: {source_type}") from e


__all__ = [
    "Adapter", "IngestResult",
    "WebsiteAdapter", "MetaAdsAdapter", "GoogleAdsAdapter", "AmazonBrandStoreAdapter",
    "TvAdsAdapter", "get_adapter", "ADAPTER_REGISTRY",
]
