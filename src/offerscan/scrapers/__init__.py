"""Registry of portal adapters (importing the modules registers them)."""

from offerscan.scrapers import (  # noqa: F401
    glassdoor,
    himalayas,
    indeed,
    infoempleo,
    infojobs,
    jobfluent,
    jooble,
    linkedin,
    manfred,
    remotive,
    talent,
    tecnoempleo,
    trabajos,
)
from offerscan.scrapers.base import REGISTRY, BaseJobSpider

__all__ = ["REGISTRY", "BaseJobSpider"]
