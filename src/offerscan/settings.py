from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from offerscan.models import JuniorProfile


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jev_api_key: SecretStr | None = None
    jev_api_base_url: str = "https://thejevai.com"
    jev_model: str = "typesafe/jev-1.13"
    jev_max_credits_per_run: float = 500
    jev_concurrency: int = 4
    laya_base_url: str = "http://127.0.0.1:8000"
    laya_model: str = "multilingual"
    laya_max_len: int = 4096
    laya_api_key: SecretStr | None = None
    infojobs_client_id: str | None = None
    infojobs_client_secret: SecretStr | None = None
    proxies: str = ""

    config_dir: Path = Path("config")
    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")

    @property
    def has_jev_key(self) -> bool:
        return bool(self.jev_api_key and self.jev_api_key.get_secret_value())

    @property
    def proxy_list(self) -> list[str]:
        return [p.strip() for p in self.proxies.split(",") if p.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "offerscan.sqlite"


class SourceConfig(BaseModel):
    enabled: bool = True
    max_pages: int = 3
    max_details: int = 30
    download_delay: float = 1.0
    robots_txt_obey: bool = True


NON_DEV_TITLE_TERMS = [
    "analista funcional",
    "consultor funcional",
    "consultor & erp|sap|odoo|cegid|peoplenet|dynamics|salesforce|navision|sage|netsuite|oracle fusion",
    "service owner",
    "tecnico de sistemas",
    "tecnico de soporte",
    "helpdesk",
    "help desk",
    "soporte",
    "administrativo",
    "carretillero",
    "mozo de almacen",
    "operario",
    "comercial",
    "teleoperador",
    "recepcionista",
    "conductor",
    "vendedor",
    "dependiente",
    "recambista",
    "pintor",
    "camarero",
    "limpieza",
    "mantenimiento",
    "electricista",
    "teacher",
    "profesor",
]


class PrefilterConfig(BaseModel):
    title_blacklist: list[str] = Field(
        default_factory=lambda: ["senior",
            "sr",
            "sr.",
            "lead",
            "principal",
            "head of",
            "architect",
            "manager",
            "arquitecto",
            "jefe",
            "responsable",
            "director",
            "lider",
            "experto",
            "tech lead",
            "team lead",
            "staff"]
    )
    max_experience_years: int = 2
    max_age_days: int = 30
    company_blacklist: list[str] = Field(default_factory=list)
    # Titles that are not programming jobs. "a & b|c" means: the title needs `a` AND (`b` OR `c`).
    non_dev_title_terms: list[str] = Field(default_factory=lambda: list(NON_DEV_TITLE_TERMS))


class SalaryConfig(BaseModel):
    monthly_payments: int = 14


class SearchConfig(BaseModel):
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=lambda: ["python"])
    locations: list[str] = Field(default_factory=list)
    work_modes: list[str] = Field(default_factory=list)  # onsite | hybrid | remote; empty = any
    junior_terms: list[str] = Field(default_factory=lambda: ["junior"])
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)
    salary: SalaryConfig = Field(default_factory=SalaryConfig)

    def enabled_sources(self) -> list[str]:
        return [name for name, cfg in self.sources.items() if cfg.enabled]


class MatchConfig(BaseModel):
    top_n: int = 20
    offer_weight: float = 0.5
    fit_weight: float = 0.5
    top_threshold: float = 60
    review_threshold: float = 45


class ScoringConfig(BaseModel):
    weights: dict[str, float] = Field(default_factory=dict)
    red_flags_discard: float = 0.7
    low_confidence_threshold: float = 0.5
    # junior_target: P(con_experiencia) >= discard -> discard; P(junior) < min or no_claro -> review.
    junior_target_discard: float = 0.6
    junior_target_min: float = 0.5
    junior_salary_max: int = 35_000  # EUR/year; salary_min above it is not a junior salary
    junior_salary_review_ratio: float = 0.85
    min_description_chars: int = 200
    match: MatchConfig = Field(default_factory=MatchConfig)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_search_config(config_dir: Path) -> SearchConfig:
    return SearchConfig.model_validate(_load_yaml(config_dir / "search.yaml"))


def load_scoring_config(config_dir: Path) -> ScoringConfig:
    return ScoringConfig.model_validate(_load_yaml(config_dir / "scoring.yaml"))


def load_juniors(config_dir: Path) -> list[JuniorProfile]:
    folder = config_dir / "juniors"
    return [
        JuniorProfile.model_validate(_load_yaml(p)) for p in sorted(folder.glob("*.yaml"))
    ]
