from .base import Adapter, IngestResult
from .website import WebsiteAdapter
from .meta_ads import MetaAdsAdapter
from .amazon_brand_store import AmazonBrandStoreAdapter

ADAPTER_REGISTRY: dict[str, type[Adapter]] = {
    "website": WebsiteAdapter,
    "meta_ads": MetaAdsAdapter,
    "amazon_brand_store": AmazonBrandStoreAdapter,
}


def get_adapter(source_type: str) -> type[Adapter]:
    try:
        return ADAPTER_REGISTRY[source_type]
    except KeyError as e:
        raise ValueError(f"unknown source type: {source_type}") from e


__all__ = [
    "Adapter", "IngestResult",
    "WebsiteAdapter", "MetaAdsAdapter", "AmazonBrandStoreAdapter",
    "get_adapter", "ADAPTER_REGISTRY",
]
