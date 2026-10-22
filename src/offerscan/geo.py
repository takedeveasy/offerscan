"""Spanish provinces and how each portal identifies them.

Verified against the real pages on 2026-10-21 (fixtures in tests/fixtures):
- Tecnoempleo: `pr=,<id>,<id>,` (ids 231..282, alphabetical `select#pr` of /ofertas-trabajo/).
- InfoJobs: path slug `/ofertas-trabajo/<slug>/informatica-telecomunicaciones/<keyword>`.
"""

from __future__ import annotations

from dataclasses import dataclass

from offerscan.pipeline.normalize import strip_accents

REMOTE = "Remoto"


@dataclass(frozen=True)
class Province:
    name: str
    tecnoempleo_id: int
    infojobs_slug: str

    @property
    def key(self) -> str:
        return strip_accents(self.name).lower()


# (name, Tecnoempleo `pr` id, InfoJobs slug)
PROVINCES: list[Province] = [
    Province(n, i, s)
    for n, i, s in [
        ("A Coruña", 231, "a-coruna"),
        ("Álava", 232, "alava-araba"),
        ("Albacete", 233, "albacete"),
        ("Alicante", 234, "alicante-alacant"),
        ("Almería", 235, "almeria"),
        ("Asturias", 236, "asturias"),
        ("Ávila", 237, "avila"),
        ("Badajoz", 238, "badajoz"),
        ("Baleares", 239, "islas-baleares-illes-balears"),
        ("Barcelona", 240, "barcelona"),
        ("Bizkaia", 241, "vizcaya-bizkaia"),
        ("Burgos", 242, "burgos"),
        ("Cáceres", 243, "caceres"),
        ("Cádiz", 244, "cadiz"),
        ("Cantabria", 245, "cantabria"),
        ("Castellón", 246, "castellon-castello"),
        ("Ceuta", 247, "ceuta"),
        ("Ciudad Real", 248, "ciudad-real"),
        ("Córdoba", 249, "cordoba"),
        ("Cuenca", 250, "cuenca"),
        ("Gipuzkoa", 251, "guipuzcoa-gipuzkoa"),
        ("Girona", 252, "girona"),
        ("Granada", 253, "granada"),
        ("Guadalajara", 254, "guadalajara"),
        ("Huelva", 255, "huelva"),
        ("Huesca", 256, "huesca"),
        ("Jaén", 257, "jaen"),
        ("La Rioja", 258, "la-rioja"),
        ("Las Palmas", 259, "las-palmas"),
        ("León", 260, "leon"),
        ("Lugo", 261, "lugo"),
        ("Lleida", 262, "lleida"),
        ("Madrid", 263, "madrid"),
        ("Málaga", 264, "malaga"),
        ("Melilla", 265, "melilla"),
        ("Murcia", 266, "murcia"),
        ("Navarra", 267, "navarra"),
        ("Ourense", 268, "ourense"),
        ("Palencia", 269, "palencia"),
        ("Pontevedra", 270, "pontevedra"),
        ("Salamanca", 271, "salamanca"),
        ("Santa Cruz de Tenerife", 272, "santa-cruz-de-tenerife"),
        ("Segovia", 273, "segovia"),
        ("Sevilla", 274, "sevilla"),
        ("Soria", 275, "soria"),
        ("Tarragona", 276, "tarragona"),
        ("Teruel", 277, "teruel"),
        ("Toledo", 278, "toledo"),
        ("Valencia", 279, "valencia-valencia"),
        ("Valladolid", 280, "valladolid"),
        ("Zamora", 281, "zamora"),
        ("Zaragoza", 282, "zaragoza"),
    ]
]

_ALIASES = {
    "vizcaya": "bizkaia",
    "guipuzcoa": "gipuzkoa",
    "la coruna": "a coruna",
    "coruna": "a coruna",
    "alacant": "alicante",
    "illes balears": "baleares",
    "islas baleares": "baleares",
    "araba": "alava",
    "castello": "castellon",
    "tenerife": "santa cruz de tenerife",
    "sta. cruz de tenerife": "santa cruz de tenerife",
}
_BY_KEY = {p.key: p for p in PROVINCES}


def find_province(name: str) -> Province | None:
    key = strip_accents(name).lower().strip()
    return _BY_KEY.get(_ALIASES.get(key, key))


def is_remote(name: str) -> bool:
    return strip_accents(name).lower().strip() in {"remoto", "remote", "teletrabajo"}


def split_locations(names: list[str]) -> tuple[list[Province], bool, list[str]]:
    """(known provinces, wants remote, unknown free-text places)."""
    provinces: list[Province] = []
    remote = False
    unknown: list[str] = []
    for name in names:
        if is_remote(name):
            remote = True
        elif (p := find_province(name)) is not None:
            if p not in provinces:
                provinces.append(p)
        elif name.strip():
            unknown.append(name.strip())
    return provinces, remote, unknown


def matches_location(location: str, work_mode: str, names: list[str]) -> bool:
    """Post-filter used when a portal cannot filter by province server-side."""
    if not names:
        return True
    provinces, wants_remote, unknown = split_locations(names)
    if wants_remote and work_mode == "remote":
        return True
    place = strip_accents(location).lower()
    wanted = [p.key for p in provinces] + [strip_accents(u).lower() for u in unknown]
    wanted += [a for a, target in _ALIASES.items() if target in [p.key for p in provinces]]
    return any(w in place for w in wanted)
