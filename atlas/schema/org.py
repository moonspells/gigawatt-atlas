"""Organizations in data/orgs.json: a JSON array of Org sorted by id (07 §3.1, §6.6)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from atlas.schema.record import ORG_ID_PATTERN, AtlasModel

OrgKind = Literal[
    "hyperscaler", "colocation", "neocloud", "crypto_hpc", "enterprise", "developer", "other"
]


class Org(AtlasModel):
    """An operator, owner, developer or tenant, with its aliases and identifiers."""

    id: str = Field(pattern=ORG_ID_PATTERN)
    name: str
    aliases: list[str] = Field(default_factory=list)
    parent_id: str | None = Field(None, pattern=ORG_ID_PATTERN)
    wikidata_qid: str | None = Field(None, pattern=r"^Q\d+$")
    sec_cik: str | None = Field(None, pattern=r"^\d{10}$")
    kind: OrgKind
