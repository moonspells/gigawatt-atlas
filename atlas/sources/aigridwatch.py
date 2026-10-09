"""AI GridWatch data center project tracker: a pipeline seed (07 §4.1, §4.3).

Reads https://aigridwatch.com/data/projects.json (CC BY 4.0; the license field is checked). Each
verified project becomes a `project` record at `locality` precision:

- the point is AI GridWatch's approximate locality coordinate, checked to lie in the state and in
  the county the locality names; a point outside that county is not used, and the row is placed
  like a row without coordinates (Gazetteer place in the county, else the county's point; a row
  without coordinates may also name a New England town or a township, a county subdivision);
- the county comes from the locality text ("Muncy Township (Lycoming County)", "Caddo Parish");
  with source coordinates, a city or municipality of the text is named only when the point lies
  in it (check_place: the Census place polygons, and for a township its Gazetteer point and area);
- a city or municipality is never named when the row's note or event log puts the site outside
  it ("north of Yerington", "near Fredericksburg", "annexation into City of Burgin"): the record
  names the county and keeps the point at county precision (outside_phrase); a row without
  coordinates whose locality names no county is placed in the county its note and log state ("in
  Stafford County"), and a Virginia independent city is its own county;
- status_history is built from the milestone dates (announced, rezoning filed, a hearing a
  decision on its day or an event of its day says was held, a decision; an approval only when
  the row names it a land-use approval or a permit), read as the row explains them: an announced
  date the row calls an LLC's registration is no announcement and one it calls a filing is the
  filing; a rezoning_filed date that is an inquiry, a report or the municipality's own act is no
  filing, and the row's own earlier filing dates the application; a vote around midnight is
  dated by its meeting. A first_reported event comes from the row's event log when its first
  report can be told (first_report), before the announced date too; entries citing placeholder
  links date nothing. A date on the 1st of a month is
  read at month precision, one on January 1 as its year. The derived `stage` goes through the
  status crosswalk (07 §4.7). When the milestones do not roll up to the stage's status, an
  `other` event records the stage as of the row's as_of date, or, for a row with no as_of, as of
  the file's generated date: an observation that sets the status and dates nothing. A row whose
  event log or note reports an approval, a groundbreaking or construction the stage has not
  reached, or leaves no application under review, is held for review, and so is one whose
  earliest milestone would date its first report later than the row shows, or is a denial, a
  withdrawal or a pause; so is a row under construction whose log reports an injunction or a
  halt, and an announced row a ban or moratorium blocks;
- size_mw keeps its figure in mw_as_stated and becomes it_mw or utility_request_mw only when the
  row's note states that basis (07 §2.4) and no entry of its log gives the campus another figure
  on that basis; a named campus gets the acreage its log gives it; a row that is a power supply
  deal or a generation facility is out of scope (07 §2.2);
- rows marked verified=false are leads, not facts, and become `unverified_upstream` review items;
- AI GridWatch republishes most of Epoch AI's US sites, and has rows of its own for some of them
  under other names. A row that is the same site as a stored Epoch record becomes a
  `possible_duplicate` item instead of a second record (EpochSites: the same name or id in the
  same state, a `source` that only that record cites, or, in the record's county, its own source
  being a link specific to that site or its street address in its name). A row tied only through
  its event log, or whose MW differ from the site's by more than x2, one that cites an Epoch AI
  page, one that only shares an organization with Epoch sites within 5 km or names the township
  an Epoch record states, and one the rules tie to several sites are held for review without
  naming one; so is AI GridWatch's copy of an Epoch site that a rule tied to its numbered
  sibling (QTS Richmond 2 to QTS Richmond 1). A row held as an Epoch record whose
  stage disagrees with that record's status also gets a `conflict` item (the stage is not
  applied). The run refuses a store without Epoch records unless --without-epoch is given, since
  `epoch` must run first;
- a row whose id begins with another row's name (AI GridWatch shifted the ids of a block of PA DEP
  rows) is held for review, since records are matched on that id;
- a reviewer releases a held row through config/overrides/aigridwatch.json (one entry per row
  id, with the checks released, a reason and the review date);
- parties hold organizations only (07 §5.3): a person, a capacity figure ("67 MW") or a
  parenthetical note ("(parcels)", "(AWS)") is never stored as a name. The "Operator/developer"
  field is the developer; an electric utility in it is left out, and so is a developer the row
  says is being replaced.

The per-project event log (events[]) is not imported in M1: only its dates, kinds and the few
markers above are read.

Importing this module does no I/O.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, TypeAdapter, ValidationError, model_validator

from atlas.crosswalk import (
    UnknownStatus,
    from_aigridwatch_stage,
    is_aigridwatch_application_stage,
)
from atlas.geo.fips import IN_SCOPE, state_by_abbr
from atlas.schema.record import (
    PLACEHOLDER_ID,
    AtlasModel,
    EventType,
    FacilityRecord,
    FuzzyDate,
    Status,
    StatusEvent,
    StatusReason,
)
from atlas.schema.rollup import (
    ACTIVE_ORDER,
    RollupError,
    apply_rollup,
    event_key,
    mw_display,
    period_start,
)
from atlas.sources.base import Candidate, ImportResult, ReviewItem, classify_source, load_input
from atlas.text import clean_text as _clean_text
from atlas.text import find_personal_data

if TYPE_CHECKING:
    from atlas.geo.counties import County, CountyIndex
    from atlas.geo.places import PlaceIndex
    from atlas.geocode import Gazetteer, GeocodeResult
    from atlas.net import FetchError, FetchResult
    from atlas.sources.base import ImportContext

PROJECTS_URL = "https://aigridwatch.com/data/projects.json"
OVERRIDES_PATH = Path("config/overrides/aigridwatch.json")
DATASET_URL = "https://aigridwatch.com/projects"
LICENSE = "CC-BY-4.0"
UPSTREAM_LICENSE = "CC BY 4.0"
CONFIDENCE = 0.70
MW_MAX = 10_000.0
ACRES_MAX = 100_000.0
S1_SUPPORTS = (
    "/canonical_name",
    "/aliases",
    "/parties",
    "/location",
    "/capacity",
    "/site",
    "/status_history",
)
REQUIRED_FIELDS = ("id", "name", "locality", "state", "stage", "verified")
# The note of the `other` event for a stage on a row without as_of (dated with the file).
UNDATED_STAGE_NOTE = (
    "AI GridWatch stage '{stage}', seen in its file on the file's date: the row has no as_of, so "
    "this is not the date the stage began"
)

_PAREN_RE = re.compile(r"^(?P<outside>.*?)\s*\((?P<inside>[^()]*)\)\s*$")
_COUNTY_RE = re.compile(r"^(?P<names>.+?)\s+(?P<kind>County|Counties|Parish|Parishes)$", re.I)
_AK_COUNTY_RE = re.compile(
    r"^(?P<names>.+?)\s+(?P<kind>City and Borough|Borough|Census Area|Municipality)$", re.I
)
_NAME_LIST_RE = re.compile(r"\s*/\s*|\s*,\s*|\s+and\s+|\s*&\s*")
_MUNICIPALITY_RE = re.compile(r"\b(?:Townships?|Boroughs?|Village)\b", re.I)
_HINT_NOISE_RE = re.compile(
    r"^(?:near|outside|north of|south of|east of|west of)\s+|\s+area$", re.I
)
_PERSON_ROLE_RE = re.compile(r"\((?:developer|owner|landowner|investor|individual)\)$", re.I)
_ORG_MARKER_RE = re.compile(
    r"\b(?:LLC|L\.L\.C\.|Inc|Corp|Corporation|Co|Company|Ltd|LP|LLP|Holdings|Group|Partners|"
    r"Development|Developers|Capital|Energy|Data|Fund|Trust|Properties|Ventures|Realty|"
    r"Center|Centre|Park|Campus|Project|Microgrids?|Solutions|Systems|Technologies|Infrastructure|"
    r"Power|Digital|Cloud|Mining|Industries|Investments?)\b\.?",
    re.I,
)
# A trailing parenthetical on a party: a role ("(parcels)"), an alias ("(AWS)") or a person's name.
_TRAILING_NOTE_RE = re.compile(r"^(?P<name>.*?\S)\s*\((?P<note>[^()]*)\)$")
# Two or three capitalized words ("Jane Example", "Jane Q. Example", "Jane O'Example").
_PERSON_NAME_RE = re.compile(
    r"^[A-Z][a-z]*(?:['\u2019-][A-Z]?[a-z]+)*\.?(?:\s+[A-Z][a-z]*(?:['\u2019-][A-Z]?[a-z]+)*\.?){1,2}$"
)
# A capacity typed into a party field ("67 MW").
_CAPACITY_RE = re.compile(r"^[\d.,]+\s*(?:MW|GW)$", re.I)
_EPOCH_HOST = "epoch.ai"
# AI GridWatch's copy of an Epoch AI site says so in its note ("Epoch AI estimates ~180 MW.").
_EPOCH_COPY_RE = re.compile(r"\bEpoch AI estimates?\b", re.I)
_SITE_NUMBER_RE = re.compile(r"^(?P<stem>.+?)[\s-]+(?P<n>\d+|[A-Z])$")
# An AI GridWatch row that shares an organization with an Epoch site this close is held for review.
NEARBY_M = 5_000.0
# A link ties a row to an Epoch site only when their MW agree within this factor (07 §6.6, Size).
MW_FACTOR = 2.0
# Precisions at which a point says where the site is, not only its county or state.
_PRECISE = frozenset({"footprint", "parcel", "site", "address", "street", "locality"})
_WORD_RE = re.compile(r"[^\W_]+")

_OUTCOMES: dict[str, tuple[Status, EventType]] = {
    "approved": ("permitted", "approved"),
    "denied": ("denied", "denied"),
    "withdrawn": ("cancelled", "withdrawn"),
    "moratorium": ("paused", "paused"),
}
_FILING_EVENT_KINDS = frozenset({"filing", "rezoning"})
# A filing or rezoning entry is the row's own application unless it is about the place's rules
# (_REPORT_CONTEXT_RE: Posey County's revision of its own data center ordinance), a property
# deal, or someone else's lawsuit, appeal or motion.
_NOT_AN_APPLICATION_RE = re.compile(
    r"\b(?:lawsuits?|sued?|sues|motions?|appeals?|appealed|complaints?|purchas\w*|acquir\w*|"
    r"bought|sold|transfer\w*|resolutions?|fees?|notices?|RFQ)\b",
    re.I,
)
_APPLICATION_RE = re.compile(
    r"\b(?:appl(?:y|ies|ied|ying|ications?)|petition(?:s|ed)?|submi(?:t|ts|tted|ssion)|filed|files|"
    r"filing|requests?|requested|(?:site|sketch|development|concept|land[- ]use) plans?|permits?)\b",
    re.I,
)
# The row says nothing has been formally proposed (Posey County: "despite no official
# proposal"): a stage that implies an application (Awaiting decision) is then announced.
_NO_APPLICATION_RE = re.compile(
    r"\b(?:no (?:official|formal) (?:site )?(?:proposal|application|plan)s?|nothing (?:has been )?"
    r"formally proposed|no (?:projects?|developments?|plans?) (?:have|has) been formally "
    r"(?:proposed|submitted|filed)|not (?:yet )?(?:been )?formally (?:proposed|submitted|filed)|"
    r"no (?:permit )?applications? (?:(?:has|have|was|were) (?:ever |yet )?been "
    r"(?:filed|submitted|received)|(?:was|were) (?:ever )?(?:filed|submitted)|received|filed)|"
    # n30, Project Zora: "with no permit applications before January 2027", "no formal
    # application or site review had been submitted", "will not submit permit applications".
    r"no (?:formal |official )?(?:permit |zoning |rezoning |site[- ]plan |land[- ]use )?"
    r"applications?(?: or [\w -]{1,30}?)? (?:has|have|had|was|were) (?:ever |yet )?(?:been )?"
    r"(?:filed|submitted|received|made)|"
    r"no (?:formal |official )?(?:permit |zoning |rezoning )?applications? (?:before|until)|"
    r"(?:will|would) not (?:submit|file) (?:any )?(?:formal |permit |zoning )?applications?|"
    r"no (?:formal )?site review (?:has|had|have|was) (?:yet )?(?:been )?(?:requested|submitted|"
    r"filed))\b",
    re.I,
)
# A decision with outcome approved is an approval to build only when the row names a land-use
# approval or a permit for it (07 §2.3, permitted): Botetourt's Board of Supervisors approved a
# performance agreement on its decided_date, an incentive deal.
_LAND_USE_OBJECT = (
    r"(?:CUP|SUP|rezonings?|rezone|re-?zoning|zoning (?:map )?(?:amendment|change|request|"
    r"petition|application)s?|conditional[- ]use|special[- ](?:use|exception)|site[- ]plans?|"
    r"development plans?|planned (?:unit )?development|PUD|(?:building|grading|"
    r"land[- ]disturbance|construction|zoning|conditional[- ]use|special[- ]use|use) permits?|"
    r"permits?|variances?|subdivision|(?:preliminary |final )?plat)"
)
_LAND_USE_APPROVAL_RE = re.compile(
    r"\b(?:voted(?:\s+[\w-]+){0,4}\s+to\s+approve|approv(?:e|ed|es|ing)|grant(?:ed|s|ing)|upheld|"
    r"issued)(?:\W+(?!(?:resolutions?|ordinances?|fees?|moratori\w*|text|bans?|agreements?|and|"
    r"but|while|so|as)\b)[\w'&-]+){0,6}?\W+" + _LAND_USE_OBJECT + r"\b"
    r"|\b" + _LAND_USE_OBJECT + r"(?:\W+[\w'&-]+){0,4}?\W+(?:was\s+|were\s+|is\s+|been\s+)?"
    r"(?:final\s+)?(?:approv(?:ed|al)|granted|issued)\b"
    r"|\b(?:rezoned|(?:voted|approval|agreed|moved)\s+(?:[\w-]+\s+){0,3}?to\s+rezone)\b",
    re.I,
)
_DATE_MENTION_RE = re.compile(
    r"\b(?:(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?(?:\s+(?P<day>\d{1,2})\b)?"
    r"(?:,?\s+(?P<year>\d{4}))?|(?P<iso>\d{4}-\d{2}-\d{2}))\b"
)
# A past hearing counts as held when an event of its day, of one of these kinds, says it was held
# or voted, and nothing in it says the hearing was scheduled, moved or continued: AI GridWatch's
# hearing_date is "the next/decisive public hearing", and hearings get moved.
_HEARING_EVENT_KINDS = frozenset({"hearing", "meeting", "vote"})
_HEARING_HELD_RE = re.compile(r"\b(?:held|voted)\b", re.I)
_HEARING_NOT_HELD_RE = re.compile(
    r"\b(?:schedul\w*|will|would|to be|anticipat\w*|expect\w*|set for|slated|postpon\w*|"
    r"continu\w*|reschedul\w*|cancel+(?:ed|led)?|moved|delay\w*|upcoming|planned)\b",
    re.I,
)
# The event log dates first_reported (first_report), at announced (or proposed for the row's own
# filings), but not for a site AI GridWatch lists as built or being built; a construction or
# operation entry describes the site as it stands, and does not date the report.
_REPORT_SKIPPED_KINDS = frozenset({"construction", "operational"})
_REPORTED_STATUSES = frozenset(
    {"announced", "proposed", "permitted", "paused", "denied", "cancelled"}
)
# A data center named in an entry; and the words of the place's rules (an ordinance, a
# moratorium, regulations, an annexation, an LLC's registration, an interconnection request).
_REPORT_SUBJECT_RE = re.compile(r"\bdata[- ]?cent(?:er|re)s?\b", re.I)
_REPORT_CONTEXT_RE = re.compile(
    r"\b(?:ordinances?|moratori\w*|regulations?|zoning code|zoning changes|zoning restrictions|"
    r"text amendment|annex\w*|registered|was formed|interconnection|paused?)\b",
    re.I,
)
# An entry about an act out of public view, which reports nothing until it is revealed (Project
# Camellia: OpenAI "circulated a mutual non-disclosure agreement ... the start of negotiations
# conducted outside public view").
_PRIVATE_RE = re.compile(
    r"\b(?:non-disclosure|NDAs?|confidential\w*|outside (?:of )?public view|behind closed doors|"
    r"in secret|secretly|privately)\b",
    re.I,
)
# An environmental review of an area, which reports the project planned there when its acreage is
# the row's (n38: Monticello's "Draft Alternative Urban Areawide Review (AUAR) for a 550-acre
# industrial area", the row's 547 acres).
_ENV_REVIEW_RE = re.compile(
    r"\b(?:AUAR|EAW|EIS|DRI|Alternative Urban Areawide Review|Environmental Assessment Worksheet|"
    r"Environmental Impact Statement|Development of Regional Impact)\b"
)
# Kinds of entries that are the place's acts: with the words above, about its rules.
_RULE_KINDS = frozenset(
    {"rezoning", "moratorium", "ordinance", "vote", "hearing", "meeting", "ruling", "policy"}
)
_REGISTRATION_RE = re.compile(
    r"\b(?:was registered|registered with|was formed|was incorporated|was organized)\b"
    r"|\b(?:LLC|L\.L\.C\.|Inc\.?|Corp\.?)\s+(?:was\s+)?registered\b",
    re.I,
)
_PROPERTY_RE = re.compile(
    r"\b(?:purchas\w*|acquir\w*|acquisition|buys?|buying|bought|sold|sale of|closed on|"
    r"zero-dollar transfer)\b",
    re.I,
)
_PLAN_RE = re.compile(
    r"\b(?:for|groundwork for|toward) (?:a|an|the|its) (?:[\w-]+ ){0,6}?(?:data cent(?:er|re)s?|"
    r"campus|facility|park|project|development)\b|\b(?:plans? to|to) (?:build|construct|develop)\b|"
    r"\bproposed\b|\bdata cent(?:er|re) (?:property|site|campus|project|development)\b",
    re.I,
)
# Another site of the same company in the same place (Compass's first Red Oak campus).
_OTHER_SITE_RE = re.compile(
    r"\b(?:existing|first|original|additional|adjacent|neighbou?ring|another|other)\s+"
    r"(?:[\w-]+\s+){0,3}?(?:campus|campuses|site|facility|data cent(?:er|re)s?|buildings?|acres)\b|"
    r"\bfully leased\b",
    re.I,
)
_PROJECT_WORDS_RE = re.compile(r"\b(?:campus|project|proposal|proposed)\b", re.I)
# Another use of the same land or place (Hexa's warehouse proposal before its data center; the
# Burkhalter Road corridor improvement project).
_OTHER_USE_RE = re.compile(
    r"\b(?:warehouses?|distribution cent(?:er|re)s?|housing|homes|residential|subdivision|"
    r"apartments?|retail|solar farm|corridor|road (?:improvement|widening)|roadway|"
    r"transportation)\b",
    re.I,
)
_ACRES_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)[- ]acres?\b", re.I)
# An entry about an event to come: its date may be the event's, not the report's.
_SCHEDULED_RE = re.compile(
    r"\b(?:schedul\w*|will|upcoming|set (?:for|to)|slated|open house|to be held|plans? to hold)\b",
    re.I,
)
_URL_DATE_RE = re.compile(
    r"/(?P<y>20\d\d)[/-](?P<m>\d{1,2}|[a-z]{3,9})[/-](?P<d>\d{1,2})(?=[/.-]|$)", re.I
)
# "filed a conditional-use application in May 2026": a filing month the row states undated.
_FILED_IN_RE = re.compile(
    r"\b(?:filed|submitted)\b[^.;]{0,80}?\bin\s+(?P<mon>January|February|March|April|May|June|"
    r"July|August|September|October|November|December)\s+(?P<year>20\d\d)\b"
)
# Words of an AI GridWatch name that do not name a site.
_GENERIC_TEXT = """
    data center centers centre datacenter datacenters campus project park business commerce
    industrial technology tech hyperscale unnamed county township borough city town village
    road roads street avenue site phase north south east west new the former power plant
    energy development facility green prime digital global international infrastructure
    capital group hub innovation district expansion proposed station nuclear steam electric
    solar mega megasite ranch farm farms properties real estate logistics mission critical
    atlas black blue red first second american united applied engineered stream tract iron
    land company partners holdings ventures investors associates
    """
_GENERIC_WORDS = frozenset(_GENERIC_TEXT.split())
# Event-log markers of a later milestone than the stage: an approval of the project's land-use
# application, or a groundbreaking. Words close before a marker that put it in the future, deny it
# or make it conditional void it.
_LOG_HEDGE_RE = re.compile(
    r"\b(?:expect\w*|anticipat\w*|plan(?:s|ned)?|could|would|will|may|might|if|once|seek\w*|"
    r"pending|recommend\w*|not|never|no|schedul\w*|set to|slated|intends?|aims?|asked|before|"
    r"until|whether)\b|n't\b",
    re.I,
)
# The decision's object, and the words between the verb and it that make it another decision ("a
# resolution raising conditional use fees", "the petition was rejected and the rezoning remained").
_LOG_OBJECT = (
    r"(?:\W+(?!(?:resolutions?|ordinances?|fees?|moratori\w*|amendments?|text|bans?|appeals?|"
    r"motions?|and|but|while|so|as)\b)[\w'-]+){0,6}?\W+(?:rezoning|conditional[- ]use|"
    r"special[- ](?:use|exception)|site[- ]plan|development plan|planned unit development|PUD)\b"
)
_LOG_APPROVAL_RE = re.compile(
    r"\b(?:voted(?:\s+[\w-]+){0,2}\s+to\s+approve|approved|approves|approving|granted|upheld)"
    + _LOG_OBJECT,
    re.I,
)
_LOG_DENIAL_RE = re.compile(
    r"\b(?:voted(?:\s+[\w-]+){0,2}\s+to\s+(?:deny|reject)|denied|denies|rejected|rejects)"
    + _LOG_OBJECT,
    re.I,
)
_LOG_GROUNDBREAKING_RE = re.compile(
    r"\b(?:groundbreaking|ground-breaking|broke ground|breaks ground|broken ground|breaking "
    r"ground|during (?:the |its )?(?:[\w-]+ ){0,3}build-?out|build-?out (?:is |was )?(?:under ?way|"
    r"ongoing|continu\w*|began|begun|started)|construction (?:disruption|activity|crews)|"
    r"(?:ongoing|active) construction|construction (?:is |was |has )?(?:been )?(?:under ?way|"
    r"ongoing|began|begun|started|continu\w*)|(?:began|begun|started) construction)\b",
    re.I,
)
# A lawsuit that seeks to undo an approval says there is one (Wolcott: "to overturn the
# rezoning").
_LOG_CHALLENGED_RE = re.compile(
    r"\b(?:su(?:e|es|ed|ing)|lawsuits?|suits?|petitions?|appeals?|challeng\w*)\b[^.;]{0,200}?"
    r"\b(?:overturn|vacate|annul|void|invalidate|revers|quash|set aside)\w*\b[^.;]{0,60}?"
    r"\b(?:rezonings?|approvals?|conditional[- ]use|special[- ]use|site[- ]plan|permits?|"
    r"variances?)\b",
    re.I,
)
_LOG_INCOMPLETE_RE = re.compile(r"\bincomplete\b", re.I)
_LOG_UPHELD_RE = re.compile(r"\buph(?:eld|olds?|olding)\b", re.I)
_LOG_MORATORIUM_RE = re.compile(
    r"\b(?:pass(?:ed|es)?|adopt(?:ed|s)?|approv(?:e|ed|es)|enact(?:ed|s)?|impos(?:e|ed|es|ing)|"
    r"vot(?:ed|es)(?:\s+[\w-]+){0,3}\s+to\s+(?:adopt|approve|pass|enact|impose))"
    r"(?:\W+[\w'-]+){0,8}?\W+(?:moratori(?:um|a)|bans?|banning|prohibit\w*\s+data cent(?:er|re)s?"
    r"(?:\s+as\s+a\s+use)?\s+(?:in\s+all\s+zones|(?:township|county|city|town)[- ]?wide))\b"
    r"|\b(?:removing|removed|removes) data cent(?:er|re)s? as (?:a )?permitted use\b",
    re.I,
)
# Words close before a moratorium that make it a demand or a draft, not an act ("residents urged
# the board to impose a moratorium").
_MORATORIUM_HEDGE_RE = re.compile(
    r"\b(?:urg\w*|call(?:s|ed|ing)? for|demand\w*|propos\w*|consider\w*|draft\w*|request\w*|"
    r"push\w* for|petition\w*)\b",
    re.I,
)
# An injunction or a halt of the works (n34: Matrix's judge "issues a temporary injunction freezing
# new construction/development on the ~5,000-acre site pending trial"), and its end.
_LOG_HALT_RE = re.compile(
    r"\b(?:(?:issu|grant|enter|order)\w*\s+(?:a\s+|an\s+)?(?:[\w-]+\s+){0,2}?injunctions?|"
    r"injunctions?\s+(?:freez|halt|block|stop|bar)\w*|"
    r"(?:halt|freez|stopp?|suspend)\w*\s+(?:all\s+|new\s+)?(?:[\w/-]+\s+){0,2}?(?:construction|"
    r"development|work)|(?:construction|work|development)\s+(?:has\s+|had\s+|was\s+|is\s+)?"
    r"(?:been\s+)?(?:halted|stopped|suspended|frozen|paused)|stop[- ]work orders?)\b",
    re.I,
)
# A halt that was refused, not ordered ("refused to grant an injunction").
_HALT_DENIED_RE = re.compile(r"\b(?:refus\w*|declin\w*|den(?:y|ies|ied)|reject\w*|without)\b", re.I)
_LOG_RESUMED_RE = re.compile(
    r"\b(?:lift\w*|dissolv\w*|overturn\w*|vacat\w*|resum\w*|restart\w*)\b", re.I
)
_LOG_EXEMPT_RE = re.compile(
    r"\b(?:exempt\w*|grandfather\w*|not (?:cover\w*|affect\w*|apply|block\w*)|does not (?:cover|"
    r"affect|apply|block)|statewide|state permitting|predat\w*)\b",
    re.I,
)
# A groundbreaking in a clause about the power supply is not the data center's.
_LOG_ENERGY_RE = re.compile(
    r"\b(?:power plants?|power station|generating station|gas plants?|substations?|transmission|"
    r"solar|battery|pipeline|turbines?)\b",
    re.I,
)
_CLAUSE_RE = re.compile(r"(?<=[.;])\s+")
# Withdrawn: the developer withdrew only when the text names the developer, the applicant or a
# party of the row as the one who withdrew; a court ruling that ended the project is litigation.
_WITHDREW_RE = r"\b(?:withdr[ae]w\w*|withdrawn|pull(?:ed|s)|dropped)\b"
_COURT_RULING_RE = re.compile(
    r"\b(?:judicially\s+(?:voided|vacated|overturned)|court\s+(?:ruled|ruling|voided|vacated|"
    r"overturned|struck)|(?:voided|vacated|overturned|struck down)\s+by\s+(?:a|the)\s+"
    r"(?:\w+\s+){0,3}(?:court|judge))\b",
    re.I,
)
_WITHDRAWAL_EVENT_KINDS = frozenset({"withdrawal", "withdrawn"})
# A row that is a power supply deal or a generation facility, not a data center site (07 §2.2).
_NOT_A_DATA_CENTER_RE = re.compile(
    r"\b(?:power purchase agreement|PPA|solar farm|solar project|power generation facility|"
    r"not a data cent(?:er|re))\b",
    re.I,
)
# size_mw and its basis: the note's figure equal to size_mw, and the words around it.
_FIGURE_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\+?\s*(MW|megawatts?|GW|gigawatts?)(?!\w)",
    re.I,
)
_IT_AFTER_RE = re.compile(r"^\W{0,3}(?:of\s+)?(?:[Cc]ritical(?:\s+IT)?|IT)\b")
_POWER_AFTER_RE = re.compile(
    r"^\W{0,3}(?:(?!(?:with|and|plus|including|for|to|in|on|at)\b)[\w-]+\s+){0,3}?"
    r"(?:power plants?|plants?|solar|nuclear|reactors?|turbines?|"
    r"generation|output|substations?|fuel[- ]cells?|batter(?:y|ies)|gas[- ]fired)\b",
    re.I,
)
_PHASE_BEFORE_RE = re.compile(r"\bphase\s*(?:\d+|[IVX]+|one)\b[^.;]{0,20}$", re.I)
_PHASE_AFTER_RE = re.compile(r"^\W{0,3}phase\b", re.I)
_SUPPLY_BEFORE_RE = re.compile(
    r"\b(?:supply|supplies|supplied|contract(?:ed|s)?|commit(?:ted|s)?|secur(?:es|ed)|"
    r"seek(?:s|ing)?|request(?:s|ed|ing)?|interconnection|grid connection)\b[^.;]*$",
    re.I,
)
_SUPPLY_AFTER_RE = re.compile(
    r"^\W{0,3}(?:of\s+)?(?:contracted|committed|requested|interconnection|grid capacity|"
    r"load request)\b",
    re.I,
)
SizeBasis = Literal["it", "utility_request", "power_source", "phase"]
# Electric utilities, which AI GridWatch's operator/developer field sometimes names for the
# utility that has to approve or supply a site (PSE&G at 100 Jersey Avenue, New Brunswick).
_UTILITY_RE = re.compile(
    r"(?:\bPSE&G|\bPSEG\b|\bPG&E|\bSDG&E|\bComEd\b|\bCon\s?Edison\b|"
    r"\bPublic Service (?:Electric|Company)\b|\bElectric (?:&|and) Gas\b|\bGas (?:&|and) Electric\b|"
    r"\bPower (?:&|and) Light\b|\bLight (?:&|and) Power\b|"
    r"\bElectric (?:Company|Cooperative|Co-?op|Membership)\b|\bEdison\b|\bDominion Energy\b|"
    r"\bDuke Energy\b|\bGeorgia Power\b|\bEntergy\b|\bXcel Energy\b|\bAppalachian Power\b|"
    r"\bFirstEnergy\b|\bPPL Electric\b|\bPECO\b|\bEvergy\b|\bAmeren\b|\bOncor\b|"
    r"\bCenterPoint\b|\bNIPSCO\b|\bAES (?:Indiana|Ohio)\b|\bAlliant Energy\b|\bWe Energies\b|"
    r"\bTennessee Valley Authority\b)",
    re.I,
)
# Ids: a generic name is told apart by its operator ("KSR" + "Unnamed Data Center").
_GENERIC_NAME_RE = re.compile(r"^unnamed data cent(?:er|re)s?$", re.I)
# Organization names compared without an alias table: suffixes dropped, then letters and digits.
_ORG_SUFFIX_RE = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|lp|llp|plc|"
    r"holdings?|group)\b\.?",
    re.I,
)


def agw_error(message: str) -> FetchError:
    """The AI GridWatch file cannot be used (license or layout). A FetchError, so `atlas import`
    reports it and exits 1; atlas.net (httpx) loads only here."""
    from atlas.net import FetchError

    return FetchError(message)


def _load_cousub_areas(ctx: ImportContext) -> dict[str, float]:
    """The county subdivisions' areas from the Census Gazetteer file the run uses (already in the
    cache: gazetteer() fetched it); none when it cannot be had, and then no township is tested
    for its extent."""
    from atlas.geo import reference
    from atlas.net import FetchError

    if not reference.cache_path(reference.COUNTY_SUBDIVISIONS, ctx.cache_dir).exists():
        return {}  # a context given its Gazetteer (tests): nothing is fetched for this
    try:
        return cousub_areas(ctx.reference_file(reference.COUNTY_SUBDIVISIONS))
    except (FetchError, OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------- small parsers


def clean_text(value: object) -> str:
    return _clean_text(str(value or ""))


def parse_day(value: object) -> date | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


AgwPrecision = Literal["day", "month", "year"]
_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


@dataclass(frozen=True)
class AgwDate:
    """An AI GridWatch date. One on the 1st of a month is that month: AI GridWatch writes
    month-level dates as YYYY-MM-01 (on 2026-10-08, 9 of 37 announced dates, 5 of 34 filing and 5
    of 39 decision dates fall on the 1st, against 0 of 31 hearing and 0 of 232 as_of dates). It
    writes a year-only date as YYYY-01-01 too ("for $165 million in 2022", "(year reported as
    2023)", Metrobloks' "In 2025 ... filed"), so January 1 is that year (parse_agw_date)."""

    start: date  # the day, or the first day of the month or year
    precision: AgwPrecision

    @property
    def end(self) -> date:
        if self.precision == "day":
            return self.start
        if self.precision == "year":
            return date(self.start.year, 12, 31)
        year, month = divmod(self.start.month, 12)
        return date(self.start.year + year, month + 1, 1) - timedelta(days=1)

    def fuzzy(self) -> dict[str, str]:
        value = self.start.isoformat()
        size = {"day": 10, "month": 7, "year": 4}[self.precision]
        return {"value": value[:size], "precision": self.precision}

    @classmethod
    def of(cls, d: FuzzyDate) -> AgwDate:
        """A record's date (a quarter is read as its first month)."""
        start = period_start(d)
        if d.precision == "day":
            return cls(start, "day")
        return cls(start, "year" if d.precision == "year" else "month")


def _names_month(text: str, month: int, year: int, *, with_year: bool) -> bool:
    """Whether the text names that month ("January 2025", "Jan. 15, 2025"; without with_year,
    also "in January")."""
    name = _MONTH_NAMES[month - 1].capitalize()  # case kept: "May" and "March" are words too
    tail = rf"(?:\s+\d{{1,2}},?)?\s+{year}" if with_year else r""
    return bool(re.search(rf"\b(?:{name}|{name[:3]}\.?){tail}\b", text))


def parse_agw_date(value: object, text: str = "", *, note: bool = False) -> AgwDate | None:
    """A date of AI GridWatch's file. On the 1st of a month it is that month, and on January 1
    that year, unless `text` names January: an event-log entry's summary, or, with note=True, the
    row's note for a milestone, which must name "January {year}". An entry whose summary gives
    only the year ("in 2022", "year reported as 2023") is that year whatever the month. Other days
    are days."""
    day = parse_day(value)
    if day is None:
        return None
    if day.day != 1:
        return AgwDate(day, "day")
    text = clean_text(text)
    year_only = not note and bool(
        re.search(rf"\b(?:year reported as|in)\s+{day.year}\b", text, re.I)
    )
    if year_only and not _names_month(text, day.month, day.year, with_year=False):
        return AgwDate(day, "year")
    if day.month == 1 and not _names_month(text, 1, day.year, with_year=note):
        return AgwDate(day, "year")
    return AgwDate(day, "month")


def milestone_date(project: dict[str, Any], key: str) -> AgwDate | None:
    """A milestone field of the row (announced, rezoning_filed, hearing_date, decided_date,
    as_of), read with the row's note (parse_agw_date)."""
    return parse_agw_date(project.get(key), clean_text(project.get("note")), note=True)


def entry_date(entry: dict[str, Any]) -> AgwDate | None:
    """An event-log entry's date, read with its summary (parse_agw_date)."""
    return parse_agw_date(entry.get("date"), clean_text(entry.get("summary")))


def parse_number(value: object) -> float | None:
    """size_mw and acres are int, float or ""; anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = clean_text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def looks_like_person(name: str) -> bool:
    """A party AI GridWatch annotates with a personal role ("X Y (developer)") and that carries no
    organization marker. Records hold organizations only (07 §5.3)."""
    return bool(_PERSON_ROLE_RE.search(name)) and not _ORG_MARKER_RE.search(
        _PERSON_ROLE_RE.sub("", name)
    )


def looks_like_person_name(text: str) -> bool:
    """Two or three capitalized words with no organization marker: a person's name."""
    text = clean_text(text)
    return bool(_PERSON_NAME_RE.match(text)) and not _ORG_MARKER_RE.search(text)


@dataclass(frozen=True)
class PartyName:
    """A party string reduced to an organization name (None when there is none)."""

    name: str | None
    person_dropped: bool = False  # a person's name was left out
    capacity: bool = False  # the string is a capacity figure, not a party


def party_name(text: str) -> PartyName:
    """The organization a party string names, without a trailing parenthetical note.

    "Example Holdings LLC (developer)" -> "Example Holdings LLC"; "Amazon (AWS)" -> "Amazon";
    "Example Ventures (Jane Example)" -> "Example Ventures", person dropped; "Jane Example
    (developer)" -> none, person dropped; "67 MW" -> none, a capacity.
    """
    text = clean_text(text).strip(" ;,")
    if _CAPACITY_RE.match(text):
        return PartyName(None, capacity=True)
    if looks_like_person(text) or find_personal_data(text):
        return PartyName(None, person_dropped=True)
    m = _TRAILING_NOTE_RE.match(text)
    if m is None:
        return PartyName(text or None)
    name = m.group("name").strip(" ;,")
    person = looks_like_person_name(m.group("note"))
    return PartyName(name or None, person_dropped=person)


def split_names(text: str, separators: str) -> list[str]:
    """Names split on the given regex, cleaned, de-duplicated, in order."""
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(separators, clean_text(text)):
        name = part.strip(" ;,")
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def filing_names(text: str) -> list[str]:
    """filing_llc split on " / " and ";"; a part that starts in lowercase is prose, not a name
    ("...LLC; applicant changed to ... in July 2025")."""
    return [n for n in split_names(text, r"\s+/\s+|\s*;\s*") if not n[0].islower()]


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


# ---------------------------------------------------------------------------- locality


@dataclass(frozen=True)
class Locality:
    """What the locality text says: a place (city or minor civil division), counties, a hint."""

    place: str | None  # the text outside the parentheses when it is not a county
    county_texts: tuple[str, ...]  # "Lycoming County", "Hays County", ...
    hint: str | None  # non-county text in parentheses ("Indianapolis", "Granbury")
    # The hint is a nearby place ("near Reno", "Granbury area"), not the one the site is in: it may
    # give a fallback point, never the city.
    hint_is_nearby: bool = False


def _county_names(text: str, state: str) -> list[str] | None:
    """["Chester County", "Montgomery County"] for "Chester / Montgomery County"; None when the
    text does not name counties. Boroughs and census areas are counties only in Alaska."""
    m = _COUNTY_RE.match(text) or (_AK_COUNTY_RE.match(text) if state == "AK" else None)
    if m is None:
        return None
    kind = m.group("kind")
    singular = {"counties": "County", "parishes": "Parish"}.get(kind.casefold(), kind)
    names = [n for n in _NAME_LIST_RE.split(m.group("names")) if n.strip()]
    return [f"{n.strip()} {singular}" for n in names]


def parse_locality(text: str, state: str) -> Locality:
    text = clean_text(text)
    outside, inside = text, ""
    m = _PAREN_RE.match(text)
    if m:
        outside, inside = m.group("outside").strip(), m.group("inside").strip()
    counties: list[str] = []
    places: list[str] = []
    for part in re.split(r"\s+/\s+", outside) if outside else []:
        names = _county_names(part, state)
        if names:
            counties.extend(names)
        elif part:
            places.append(part)
    hint: str | None = None
    nearby = False
    if inside:
        names = _county_names(inside, state)
        if names:
            counties.extend(names)
        else:
            # A compound parenthesis is read part by part, like the text outside it: "(Pittsylvania
            # County / Danville)" names the county, and the rest is the hint (n42).
            rest: list[str] = []
            for part in re.split(r"\s+/\s+", inside):
                names = _county_names(part, state)
                if names:
                    counties.extend(names)
                elif part:
                    rest.append(part)
            first = (" / ".join(rest)).split(",", 1)[0]
            hint = _HINT_NOISE_RE.sub("", _HINT_NOISE_RE.sub("", first)).strip() or None
            nearby = bool(_HINT_NOISE_RE.search(first))
    place = " / ".join(places) or None
    if place and place.casefold().startswith("city of "):
        place = place[len("city of ") :].strip() or None
    return Locality(place, tuple(counties), hint, nearby)


def named_counties(loc: Locality, state: str, counties: CountyIndex) -> list[County]:
    """The counties the locality names that the county file knows, in the order named."""
    found: dict[str, County] = {}
    for name in loc.county_texts:
        c = counties.by_name(state, name)
        if c is not None:
            found.setdefault(c.fips, c)
    return list(found.values())


def resolve_county(
    loc: Locality,
    state: str,
    counties: CountyIndex,
    point: tuple[float, float] | None,
) -> County | None:
    """The named county that contains the point (with the validate tolerance; of two that do, near
    their border, the one the point lies in); without a point, the named county when there is one.
    Else None: a record never names a county its point is outside of."""
    found = named_counties(loc, state, counties)
    if point is None:
        return found[0] if len(found) == 1 else None
    inside = [c for c in found if counties.contains(c.fips, point[0], point[1])]
    if len(inside) > 1:
        exact = counties.lookup(point[0], point[1])
        inside = [c for c in inside if exact is not None and c.fips == exact.fips]
    return inside[0] if len(inside) == 1 else None


# The row's own words putting the site outside a place (n0, n5, n32): "Mason Valley north of
# Yerington", "a site 11 miles north of Fort Stockton", "outside Socorro", "near Fredericksburg",
# "sought annexation into City of Burgin". A record never names such a place as its city or
# municipality, whatever its point says: AI GridWatch's point is often the town's own.
_DIRECTION = r"(?:north|south|east|west)(?:-?(?:east|west))?(?:ern)?"
_DISTANCE = (
    r"(?:(?:about|approximately|roughly|some|nearly|~)\s*)?\d+(?:\.\d+)?\+?\s*-?\s*"
    r"(?:miles?|mi\.?|km|kilometers?)\s+"
)
_TOWN_OF = r"(?:the\s+)?(?:(?:city|town|village|borough)\s+(?:of\s+)?)?"
# A capitalized word after the name makes it another name: a road, a park, a council or the
# county of the same name ("south of Tonganoxie Business Park", "outside the Sulphur Springs City
# Council meeting", "near Liberty Road"); and a building of the place is in it ("a press event
# outside Lansing town hall").
_NOT_THE_PLACE = (
    r"(?!\s+(?-i:[A-Z]))(?!\s+(?:town|city|village|borough|township|county)\s+(?:halls?|"
    r"council|board|offices?|buildings?|meetings?|chambers?|government|officials?)\b)"
)


def _outside_re(place: str) -> re.Pattern[str]:
    name = re.escape(place)
    return re.compile(
        rf"(?:\b{_DISTANCE})?\b{_DIRECTION}\s+of\s+{_TOWN_OF}{name}\b{_NOT_THE_PLACE}"
        rf"|\b(?:near|outside(?:\s+of)?|on the outskirts of|in the vicinity of|close to|beyond)"
        rf"\s+{_TOWN_OF}{name}\b{_NOT_THE_PLACE}"
        rf"|\b{_DISTANCE}(?:from|outside(?:\s+of)?)\s+(?:downtown\s+)?{_TOWN_OF}{name}\b"
        rf"{_NOT_THE_PLACE}"
        rf"|\bannex\w*\b[^.;]{{0,80}}?\b(?:into|to|by)\s+{_TOWN_OF}{name}\b",
        re.I,
    )


def outside_phrase(project: dict[str, Any], place: str) -> tuple[str, str] | None:
    """(the phrase, where it is) when the row's note or an event-log entry puts the site outside
    the place ("north of Yerington", "near Fredericksburg"); None when nothing does."""
    if not place:
        return None
    pattern = _outside_re(place)
    texts = [("its note", clean_text(project.get("note")))] + [
        (
            f"its event-log entry of {clean_text(e.get('date')) or 'no date'}",
            clean_text(e.get("summary")),
        )
        for e in row_events(project)
    ]
    for where, text in texts:
        m = pattern.search(text)
        if m:
            return m.group(0), where
    return None


# "on 82 acres in Stafford County": the county a row's note or event log places the site in (n1).
_STATED_COUNTY_RE = re.compile(
    r"\bin\s+((?:[A-Z][\w'.-]*\s+){1,3}?)(County|Parish)\b(?!\s+(?:line|lines|border)\b)"
)


def stated_county(project: dict[str, Any], state: str, counties: CountyIndex) -> County | None:
    """The one county of the state that the row's note and event log place the site in ("on 82
    acres in Stafford County"), when every county they place it in is that one; None otherwise.
    A clause about another site of the company (_OTHER_SITE_RE) does not count."""
    texts = [clean_text(project.get("note"))] + [
        clean_text(e.get("summary")) for e in row_events(project)
    ]
    found: dict[str, County] = {}
    for clause in (c for text in texts for c in _CLAUSE_RE.split(text)):
        if _OTHER_SITE_RE.search(clause):
            continue
        for m in _STATED_COUNTY_RE.finditer(clause):
            words = m.group(1).split()
            # The longest run of words before "County" that names one ("Prince William County").
            for k in range(len(words)):
                c = counties.by_name(state, " ".join(words[k:]) + " " + m.group(2))
                if c is not None:
                    found.setdefault(c.fips, c)
                    break
    return next(iter(found.values())) if len(found) == 1 else None


# A township is kept for a point within this many equal-area radii of its Gazetteer point (a
# square's corner is 1.25 radii from its centre): the files the import uses have no township
# polygons, so this is the test a point far outside the named township fails (Hanover Township's
# point lay 8.8 km from it, 1.4 radii, in Jefferson Township).
TOWNSHIP_RADII = 1.25
# States where every incorporated place is a municipality of its own, outside any township (a
# Pennsylvania or New Jersey borough or city): a point in one is in no township.
_SEPARATE_PLACE_STATES = frozenset({"PA", "NJ"})
# States whose towns are the municipality even where a village has the same name (Lansing, NY).
_TOWN_STATES = frozenset({"NY", "CT", "MA", "ME", "NH", "RI", "VT"})
_KIND_RE = re.compile(r"\s+(?:Townships?|Boroughs?|Villages?|Towns?|City)$", re.I)
_PLURAL_KIND_RE = re.compile(r"\s+(Townships|Boroughs|Villages)$", re.I)


@dataclass(frozen=True)
class PlaceCheck:
    """The city and municipality of a row with source coordinates, kept from the locality text
    only when the point lies in them, and the names left out with why."""

    city: str | None
    municipality: str | None
    dropped: tuple[tuple[str, str], ...] = ()


def place_parts(place: str) -> list[tuple[str, str | None]]:
    """The places a locality's place text names, each with its own parenthesis: "North Beaver &
    Mahoning townships" -> North Beaver Township, Mahoning Township; "Warren Township
    (Indianapolis) / Irvington" -> (Warren Township, Indianapolis), (Irvington, None)."""
    parts = [p.strip() for p in re.split(r"\s+/\s+|\s*&\s*|\s+and\s+", place) if p.strip()]
    plural = _PLURAL_KIND_RE.search(parts[-1]) if parts else None
    if plural:
        kind = plural.group(1)[:-1].capitalize()
        parts[-1] = parts[-1][: plural.start()]
        parts = [p if _KIND_RE.search(p) else f"{p} {kind}" for p in parts]
    out: list[tuple[str, str | None]] = []
    for part in parts:
        m = _PAREN_RE.match(part)
        out.append((m.group("outside"), m.group("inside")) if m else (part, None))
    return out


def check_place(
    loc: Locality,
    state: str,
    county_fips: str | None,
    lat: float,
    lon: float,
    gazetteer: Gazetteer,
    places: PlaceIndex,
    areas: Mapping[str, float],
) -> PlaceCheck:
    """The city and municipality of a row whose point is its own (n26, n31): from the locality
    text, checked against the Census place that contains the point.

    A city is kept when it is a Census place that contains the point; when the text's place is
    neither a Census place nor a county subdivision and its parenthesis names the place that
    contains the point ("Martindale-Brightwood (Indianapolis)"), that one. A borough, village or
    city named as a municipality must likewise contain the point. A township or town (a county
    subdivision; in New York and New England a town even where a village shares its name, as
    Lansing does) is kept unless another municipality's polygon contains the point (Smithfield
    Township's point lies in East Stroudsburg borough) or the point lies more than TOWNSHIP_RADII
    equal-area radii from its Gazetteer point. Of a compound text ("North Beaver & Mahoning
    townships") only the one place that passes is kept. A nearby place ("near Reno") is never
    named."""
    from atlas.geocode import distance_m

    if not loc.place:
        return PlaceCheck(None, None)
    inside = places.containing(lat, lon)
    if inside is not None and inside.state_abbr != state:
        inside = None
    city: list[str] = []
    municipality: list[str] = []
    dropped: list[tuple[str, str]] = []
    where = (
        f"the point lies in {inside.lsad_name}" if inside else "the point lies in no Census place"
    )

    def contains(name: str) -> bool:
        place = gazetteer.place_entry(state, name)
        return place is not None and inside is not None and place.geoid == inside.geoid

    def town_ok(name: str, base: str) -> tuple[bool, str]:
        sub = gazetteer.cousub_entry(state, base, county_fips) or gazetteer.cousub_entry(
            state, name, county_fips
        )
        if sub is None:
            return False, "it is no Census place or county subdivision there"
        if (
            inside is not None
            and inside.incorporated
            and state in _SEPARATE_PLACE_STATES
            and normalize_name(inside.base_name) != normalize_name(sub.base_name)
        ):
            return False, f"{where}, another municipality"
        area = areas.get(sub.geoid)
        if area:
            radius = (area / 3.141592653589793) ** 0.5
            far = distance_m(lat, lon, sub.lat, sub.lon)
            if far > TOWNSHIP_RADII * radius:
                return False, (
                    f"the point is {far / 1000:.1f} km from its Gazetteer point, beyond its "
                    f"extent ({radius / 1000:.1f} km equal-area radius)"
                )
        return True, ""

    parts = place_parts(loc.place)
    if len(parts) == 1 and parts[0][1] is None and not loc.hint_is_nearby:
        parts = [(parts[0][0], loc.hint)]  # "Martindale-Brightwood (Indianapolis)"
    for name, hint in parts:
        base = _KIND_RE.sub("", name).strip()
        is_town = state in _TOWN_STATES and gazetteer.cousub_entry(state, base, county_fips)
        if _MUNICIPALITY_RE.search(name) or is_town:
            borough = gazetteer.place_entry(state, name) if _MUNICIPALITY_RE.search(name) else None
            if (
                borough is not None
                and borough.incorporated
                and not re.search(r"\btownships?\b", name, re.I)
            ):
                if contains(name):
                    municipality.append(name)
                else:
                    dropped.append((name, where))
                continue
            ok, why = town_ok(name, base)
            if ok:
                municipality.append(base if is_town and not _MUNICIPALITY_RE.search(name) else name)
            else:
                dropped.append((name, why))
            if is_town and contains(base) and base not in city:
                city.append(base)
            continue
        if contains(name):
            city.append(name)
            continue
        known = gazetteer.place_entry(state, name) is not None or gazetteer.cousub_entry(
            state, name, county_fips
        )
        if not known and hint and not _HINT_NOISE_RE.search(hint) and contains(hint):
            city.append(inside.base_name if inside else hint)
            continue
        dropped.append((name, where if known else f"{where}, and it is no Census place there"))
    if len(municipality) > 1:
        dropped.extend(
            (m, "the locality names several, and the point does not tell which")
            for m in municipality
        )
        municipality = []
    if len(city) > 1:
        dropped.extend(
            (c, "the locality names several, and the point does not tell which") for c in city
        )
        city = []
    return PlaceCheck(
        city[0] if city else None, municipality[0] if municipality else None, tuple(dropped)
    )


def normalize_name(name: str) -> str:
    from atlas.geocode import normalize_place

    return normalize_place(name)


def _siblings(a: str, b: str) -> bool:
    """Two names of one campus's numbered sites: the same words before a different trailing
    number or letter ("QTS Richmond 2", "QTS Richmond 1")."""
    ma, mb = _SITE_NUMBER_RE.match(clean_text(a)), _SITE_NUMBER_RE.match(clean_text(b))
    return bool(
        ma
        and mb
        and ma.group("stem").casefold() == mb.group("stem").casefold()
        and ma.group("n").casefold() != mb.group("n").casefold()
    )


def _place_key(name: str) -> str:
    """A place's name for comparison: "Salem Township" and "Salem township" are "salem"."""
    return normalize_name(_KIND_RE.sub("", clean_text(name)))


def cousub_areas(path: Path) -> dict[str, float]:
    """Land plus water area (m2) of every county subdivision in the Census Gazetteer file, by
    GEOID, for the township extent test (check_place)."""
    from atlas.geocode import _read_gazetteer

    out: dict[str, float] = {}
    for r in _read_gazetteer(path, None):
        try:
            out[r["GEOID"]] = float(r.get("ALAND") or 0) + float(r.get("AWATER") or 0)
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------- status


def row_events(project: dict[str, Any]) -> list[dict[str, Any]]:
    events = project.get("events")
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


# An event-log link that is a placeholder, not a source (n10: Drox Rural Hall's nine
# ".../article_example.html" links, which return 404).
_PLACEHOLDER_URL_RE = re.compile(
    r"/article_example\d*\.html?(?:[?#].*)?$|^https?://(?:www\.)?example\.(?:com|org|net)(?:/|$)",
    re.I,
)


def placeholder_source(entry: dict[str, Any]) -> bool:
    """Whether the entry's source is a placeholder link: the entry is then unsourced."""
    return bool(_PLACEHOLDER_URL_RE.search(clean_text(entry.get("source"))))


def dating_events(project: dict[str, Any]) -> list[dict[str, Any]]:
    """The event-log entries that may date the record (a first report, a filing, a hearing held,
    a land-use approval): every entry but those whose source is a placeholder link (n10)."""
    return [e for e in row_events(project) if not placeholder_source(e)]


def _clause_of_day(clause: str, day: AgwDate) -> bool:
    """Whether a clause of the note is about that day: it names it ("May 22, 2026", "Aug 12",
    "2025-01-28"), or names no date at all. Botetourt's "Grading permit issued Aug 2026" is not
    about its decision of 2025-06-24."""
    mentions = list(_DATE_MENTION_RE.finditer(clause))
    for m in mentions:
        if m.group("iso"):
            if parse_day(m.group("iso")) == day.start:
                return True
            continue
        month = [n[:3] for n in _MONTH_NAMES].index(m.group("mon")[:3].casefold()) + 1
        if month != day.start.month:
            continue
        if m.group("year") and int(m.group("year")) != day.start.year:
            continue
        if m.group("day") and day.precision == "day" and int(m.group("day")) != day.start.day:
            continue
        return True
    return not mentions


def _same_period(a: AgwDate, b: AgwDate) -> bool:
    return a.start <= b.end and b.start <= a.end


def land_use_approval(project: dict[str, Any]) -> str | None:
    """The text that names the decision of decided_date (outcome approved) as a land-use approval
    or a permit: an event-log entry of that day, or a clause of the note about that day, with an
    approval of a rezoning, conditional or special use, special exception, site plan, development
    plan, PUD, variance, plat or permit. None when nothing does (an incentive, a development or
    performance agreement, a tax abatement)."""
    decided = milestone_date(project, "decided_date")
    if decided is None:
        return None
    for e in dating_events(project):
        day = entry_date(e)
        summary = clean_text(e.get("summary"))
        if day is not None and _same_period(day, decided) and _LAND_USE_APPROVAL_RE.search(summary):
            return summary
    for clause in _CLAUSE_RE.split(clean_text(project.get("note"))):
        if _LAND_USE_APPROVAL_RE.search(clause) and _clause_of_day(clause, decided):
            return clause
    return None


def unnamed_approval(project: dict[str, Any]) -> AgwDate | None:
    """decided_date, when its outcome is approved and nothing names it a land-use approval or a
    permit (land_use_approval): it is then no `approved` event."""
    decided = milestone_date(project, "decided_date")
    outcome = clean_text(project.get("outcome")).casefold()
    if decided is None or outcome != "approved" or land_use_approval(project) is not None:
        return None
    return decided


def late_announcement(project: dict[str, Any]) -> tuple[date, str] | None:
    """(announced, the earlier milestone) when AI GridWatch dates the announcement after a filing,
    hearing or decision: announcing then would be a backward move (07 §2.3)."""
    announced = milestone_date(project, "announced")
    if announced is None:
        return None
    earlier = [
        (day.start, key)
        for key in ("rezoning_filed", "hearing_date", "decided_date")
        if (day := milestone_date(project, key)) is not None and day.end < announced.start
    ]
    return (announced.start, min(earlier)[1]) if earlier else None


def hearing_held(project: dict[str, Any], hearing: AgwDate) -> bool:
    """Whether a milestone or an event says the hearing on that day took place: a decision dated
    that day, or an event of that day (hearing, meeting or vote, with a source) that says it was
    held or voted and does not say it was scheduled, moved or continued. A past hearing_date alone
    does not: AI GridWatch's field is the next or decisive hearing, and hearings get moved."""
    if hearing.precision != "day":
        return False
    outcome = clean_text(project.get("outcome")).casefold()
    decided = decision_day(project).day
    if outcome in _OUTCOMES and (
        parse_day(project.get("decided_date")) == hearing.start
        or (decided is not None and decided.precision == "day" and decided.start == hearing.start)
    ):
        return True
    for e in dating_events(project):
        if parse_day(e.get("date")) != hearing.start or not clean_text(e.get("source")):
            continue
        summary = clean_text(e.get("summary"))
        if (
            clean_text(e.get("kind")).casefold() in _HEARING_EVENT_KINDS
            and _HEARING_HELD_RE.search(summary)
            and not _HEARING_NOT_HELD_RE.search(summary)
        ):
            return True
    return False


def unconfirmed_hearing(project: dict[str, Any], today: date) -> AgwDate | None:
    """A hearing_date on or before today that nothing in the row says was held (hearing_held)."""
    hearing = milestone_date(project, "hearing_date")
    if hearing is None or hearing.start > today or hearing_held(project, hearing):
        return None
    return hearing


# ---------------------------------------------------------------------------- milestones the row explains
#
# AI GridWatch's milestone fields are checked against the row's own note and event log (fifth fix
# round): an announced date the row calls an LLC's registration or a filing, a rezoning_filed date
# that is an inquiry, a report or the municipality's own act, or later than the row's own filing,
# and a decision taken around midnight at the end of the previous day's meeting.

# The row says only an inquiry was made (n40: Abei Energy "emailed the Starke County Plan
# Commission asking about rezoning two parcels"; "before Abei's proposal advanced to a rezoning
# vote").
_INQUIRY_RE = re.compile(
    r"\b(?:ask(?:s|ed|ing)?|inquir(?:es|ed|ing)|emailed)\b[^.;]{0,80}?\babout\s+(?:a\s+)?"
    r"(?:re-?zoning|zoning|permits?|a data cent(?:er|re))\b"
    r"|\bbefore\b[^.;]{0,60}?\b(?:proposal|application|request|petition)\s+advanced\b",
    re.I,
)
# The municipality's own procedure, not an application (n33: Smithfield Township's Resolution 621,
# "Curative amendment application filed; 180-day MPC review period begins").
_MUNICIPAL_ACT_RE = re.compile(
    r"\bresolution\s+(?:no\.?\s*)?\d+|\b(?:review|cure|comment)\s+period\s+begins\b|"
    r"\b(?:township|county|city|borough|town|village)\s+initiat\w*\b",
    re.I,
)
# An entry that reports a filing rather than makes it (n33: Site Layer 4's "Rezoning application
# revealed", the day of the article).
_REVEALED_RE = re.compile(
    r"\b(?:applications?|requests?|petitions?|filings?)\b[^.;]{0,40}?\b(?:revealed|reported|"
    r"disclosed|surfaced|came to light|made public)\b|\b(?:revealed|reported|disclosed)\b"
    r"[^.;]{0,40}?\b(?:applications?|requests?|petitions?|filings?)\b",
    re.I,
)
# An LLC's registration, which is no report of a project (n31: Andover's "registered Andover HPC
# Development in December 2025").
_REGISTERED_RE = re.compile(r"\b(?:registered|incorporated|was formed|was organized)\b", re.I)
_MIDNIGHT_RE = re.compile(
    r"\b(?:around|past|after|near(?:ly)?|shortly (?:after|before)|just (?:after|before)|at|"
    r"until|close to)\s+midnight\b",
    re.I,
)


def _days_named(text: str, year: int) -> list[date]:
    """The days a text names ("April 15, 2026", "July 6", "2026-07-06"); a day without its year
    is in `year`."""
    out: list[date] = []
    for m in _DATE_MENTION_RE.finditer(text):
        if m.group("iso"):
            if (d := parse_day(m.group("iso"))) is not None:
                out.append(d)
            continue
        if not m.group("day"):
            continue
        month = [n[:3] for n in _MONTH_NAMES].index(m.group("mon")[:3].casefold()) + 1
        try:
            out.append(date(int(m.group("year") or year), month, int(m.group("day"))))
        except ValueError:
            continue
    return out


_DAY_TEXT = (
    r"(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:,?\s+\d{4})?|"
    r"\d{4}-\d{2}-\d{2})"
)
# A day named as a filing's: "filed a zoning permit application on April 15, 2026", "Monticello
# Tech's July 6 land-use applications".
_FILING_DAY_RE = re.compile(
    rf"\b(?:filed|submitted|applied)\b[^.;,]{{0,80}}?\b(?:on|dated)\s+(?P<a>{_DAY_TEXT})"
    rf"|(?P<b>{_DAY_TEXT})(?:'s)?\s+(?:[\w-]+\s+){{0,2}}?(?:applications?|filings?|submissions?)\b"
)


def _filing_days_named(text: str, year: int) -> list[date]:
    """The days a text names as a filing's (_FILING_DAY_RE); a day without its year is in
    `year`."""
    return [
        d
        for m in _FILING_DAY_RE.finditer(text)
        for d in _days_named(m.group("a") or m.group("b"), year)
    ]


def _names_period(text: str, day: AgwDate) -> bool:
    """Whether the text names that day (or, for a month, that month and year)."""
    if day.precision == "day":
        return day.start in _days_named(text, day.start.year)
    if day.precision == "month":
        return _names_month(text, day.start.month, day.start.year, with_year=True)
    return False


def _day_texts(project: dict[str, Any], day: AgwDate) -> tuple[list[str], list[str]]:
    """(the summaries of the event-log entries of that day, the note's clauses that name it)."""
    entries = [
        clean_text(e.get("summary"))
        for e in dating_events(project)
        if (d := entry_date(e)) is not None
        and d.start == day.start
        and d.precision == day.precision
    ]
    clauses = [
        c for c in _CLAUSE_RE.split(clean_text(project.get("note"))) if _names_period(c, day)
    ]
    return entries, clauses


def _filing_text(text: str) -> bool:
    """A text in which an applicant files ("filed a zoning permit application", "submitted its
    application")."""
    return bool(_FILED_VERB_RE.search(text) and _APPLICATION_RE.search(text)) or bool(
        re.search(r"\bapplications?\s+(?:was\s+|were\s+)?(?:filed|submitted)\b", text, re.I)
    )


def _registration_text(project: dict[str, Any], text: str) -> bool:
    """A text about an LLC's or a company's registration (not a TDLR/TABS building registration,
    which is a filing): "Andover HPC Development LLC registered at 248 Stickles Pond Road"."""
    if not _REGISTERED_RE.search(text) or _filing_text(text):
        return False
    if re.search(r"\b(?:TDLR|TABS|Department of Licensing)\b", text):
        return False
    names = filing_names(clean_text(project.get("filing_llc")))
    return bool(
        re.search(r"\b(?:LLC|L\.L\.C\.|Inc|Corp|entity|entities|company)\b", text, re.I)
        or any(_names_org(text, n) for n in names)
    )


def _own_application(project: dict[str, Any], entry: dict[str, Any]) -> bool:
    """An own filing entry (own_filing) that names the project or one of the row's organizations,
    and no electric utility: Plaza 500's "Dominion Energy filed its application ... for the
    Edsall transmission line" is the utility's."""
    summary = clean_text(entry.get("summary"))
    return (
        own_filing(entry)
        and not _UTILITY_RE.search(summary)
        and (
            names_project(project, summary) is not None
            or any(_names_org(summary, org) for org in _row_orgs(project))
        )
    )


@dataclass(frozen=True)
class Announcement:
    """AI GridWatch's announced date as the row explains it: the day to import as announced
    (None when it is not one), or the filing it is, and why it was set aside."""

    day: AgwDate | None
    filing: AgwDate | None = None
    doubt: str | None = None


def announcement(project: dict[str, Any]) -> Announcement:
    """The announced milestone, unless the row explains its day as something else (n31): an LLC's
    registration (Andover: "National Land Developers registered Andover HPC Development in
    December 2025"), which is no report, or a filing (Muncy: "filed a zoning permit application
    on April 15, 2026"), which is imported as application_filed. The row's entries of that day,
    or else the note's clauses that name it, must all say so."""
    announced = milestone_date(project, "announced")
    if announced is None:
        return Announcement(None)
    entries, clauses = _day_texts(project, announced)
    texts = entries or clauses
    if not texts:
        return Announcement(announced)
    if all(_registration_text(project, t) for t in texts):
        return Announcement(
            None,
            doubt=f"the row explains it as an LLC's registration ({texts[0][:160]!r}), which is no "
            "report of the project",
        )
    filed = [
        e
        for e in dating_events(project)
        if _own_application(project, e) and entry_date(e) == announced
    ]
    if (entries and len(filed) == len(entries)) or (
        not entries and all(_filing_text(t) for t in clauses)
    ):
        return Announcement(
            None,
            filing=announced,
            doubt=f"the row explains it as the day of a filing ({texts[0][:160]!r}): it is "
            "imported as the application's filing, not as an announcement",
        )
    return Announcement(announced)


@dataclass(frozen=True)
class Filing:
    """AI GridWatch's rezoning_filed date as the row explains it: the application_filed date to
    import (None when there is none), whether the row shows an application, and why the date is
    not imported as given."""

    day: AgwDate | None
    filed: bool
    doubt: str | None = None


def rezoning_filing(project: dict[str, Any]) -> Filing:
    """rezoning_filed checked against the row's own text (n33, n40). It is no filing when the note
    says only an inquiry was made (Abei Energy); not the application's day when the entries of
    that day are the municipality's own procedure (Smithfield's curative amendment resolution and
    its "review period begins") or only report the application ("Rezoning application revealed":
    Site Layer 4 had applied before); and when the row's own filing entries, a day they or the
    note name for a filing, or an announced date the row calls a filing, are earlier (Monticello
    Tech's "July 6 land-use applications" against 2026-07-07; Muncy's April 15 zoning permit
    application against its April 28 conditional use application), the earliest of those is the
    application's day."""
    stated = milestone_date(project, "rezoning_filed")
    note = clean_text(project.get("note"))
    if stated is None:
        early = announcement(project).filing
        return Filing(early, early is not None)
    inquiry = _INQUIRY_RE.search(note)
    if inquiry is not None and not _filing_text(note):
        return Filing(
            None,
            False,
            f"the row's note says only an inquiry was made ({inquiry.group(0)!r}), not an "
            "application",
        )
    entries, _ = _day_texts(project, stated)
    if entries and all(_MUNICIPAL_ACT_RE.search(t) for t in entries):
        return Filing(
            None,
            False,
            f"its entry of that day is the municipality's own procedure ({entries[0][:160]!r}), "
            "not the developer's application",
        )
    if entries and all(_REVEALED_RE.search(t) and not _FILED_VERB_RE.search(t) for t in entries):
        return Filing(
            None,
            True,
            f"its entry of that day only reports the application ({entries[0][:160]!r}), which "
            "was filed earlier on a day the row does not give",
        )
    candidates: list[tuple[AgwDate, str]] = []
    for e in dating_events(project):
        if not _own_application(project, e) or (d := entry_date(e)) is None:
            continue
        candidates.append((d, f"its own filing entry of {d.fuzzy()['value']}"))
        summary = clean_text(e.get("summary"))
        candidates.extend(
            (AgwDate(n, "day"), f"the filing day its entry of {d.fuzzy()['value']} names")
            for n in _filing_days_named(summary, d.start.year)
            if n <= d.end
        )
    candidates.extend(
        (AgwDate(n, "day"), "a filing day its note names")
        for n in _filing_days_named(note, stated.start.year)
    )
    early = announcement(project).filing
    if early is not None:
        candidates.append((early, "its announced date, which the row explains as a filing"))
    earlier = [(d, why) for d, why in candidates if d.end < stated.start]
    if earlier:
        day, why = min(earlier, key=lambda t: (t[0].start, t[0].precision != "day"))
        return Filing(
            day,
            True,
            f"{why} ({day.fuzzy()['value']}) is earlier, so the application's filing is dated then",
        )
    return Filing(stated, True)


@dataclass(frozen=True)
class Decision:
    """decided_date as the row explains it: the day to import, and the text that moved it."""

    day: AgwDate | None
    moved_by: str | None = None


def decision_day(project: dict[str, Any]) -> Decision:
    """decided_date, or the day before it when the row says the vote came around midnight at the
    end of that day's meeting (n37: Red Oak's council "approved the rezoning 4-1 around midnight
    following the May 11 meeting", decided_date 2026-05-12): a vote is dated by its meeting."""
    decided = milestone_date(project, "decided_date")
    if decided is None or decided.precision != "day":
        return Decision(decided)
    before = decided.start - timedelta(days=1)
    texts = [clean_text(project.get("note"))] + [
        clean_text(e.get("summary"))
        for e in dating_events(project)
        if parse_day(e.get("date")) in (decided.start, before)
    ]
    for clause in (c for t in texts for c in _CLAUSE_RE.split(t)):
        named = _days_named(clause, decided.start.year)
        if _MIDNIGHT_RE.search(clause) and before in named and decided.start not in named:
            return Decision(AgwDate(before, "day"), clause[:160])
    return Decision(decided)


def milestone_events(project: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """Events from the milestone dates, in date order; dates after today are planned, a date on
    the 1st of a month is that month (January 1, that year). An announcement dated after a
    later-stage milestone is left out (late_announcement), and so are a past hearing nothing says
    was held (hearing_held) and an approval nothing names a land-use approval or a permit
    (unnamed_approval). The announced, rezoning_filed and decided_date fields are read as the row
    explains them (announcement, rezoning_filing, decision_day)."""
    found: list[tuple[AgwDate, int, Status, EventType, str | None]] = []
    announced = announcement(project).day
    if announced and late_announcement(project) is None:
        found.append((announced, 0, "announced", "announced", None))
    filing = rezoning_filing(project)
    if filing.day:
        note = None
        if filing.day != milestone_date(project, "rezoning_filed"):
            note = "Dated by the row's own filing, not its rezoning_filed date"
        found.append((filing.day, 1, "proposed", "application_filed", note))
    hearing = milestone_date(project, "hearing_date")
    if hearing and hearing.start > today:
        found.append((hearing, 2, "proposed", "hearing_scheduled", None))
    elif hearing and hearing_held(project, hearing):
        found.append((hearing, 2, "proposed", "hearing_held", None))
    decision = decision_day(project)
    outcome = clean_text(project.get("outcome")).casefold()
    if decision.day and outcome in _OUTCOMES and unnamed_approval(project) is None:
        status, event = _OUTCOMES[outcome]
        note = None
        if decision.moved_by is not None:
            note = "Dated by its meeting: the row says the vote came around midnight at its end"
        found.append((decision.day, 3, status, event, note))
    found.sort(key=lambda t: (t[0].start, t[1]))
    return [
        {
            "seq": i + 1,
            "status": status,
            "event": event,
            "as_of": day.fuzzy(),
            "planned": day.start > today,
            "source_ids": ["s1"],
            **({"note": note} if note else {}),
        }
        for i, (day, _, status, event, note) in enumerate(found)
    ]


def _project_name(project: dict[str, Any]) -> str:
    """The row's name without a trailing parenthesis, or "" for a generic one ("Unnamed Data
    Center")."""
    name = clean_text(project.get("name"))
    m = _PAREN_RE.match(name)
    name = m.group("outside") if m else name
    return "" if _GENERIC_NAME_RE.match(name) else name


def _row_orgs(project: dict[str, Any]) -> list[str]:
    """The organizations the row names (operator/developer, owner, tenant, filing LLCs), for
    matching the row's own text only (never stored from here)."""
    out: list[str] = []
    for role in ("operator", "owner", "tenant", "filing_llc"):
        for part in split_names(clean_text(project.get(role)), r"\s+/\s+|\s*;\s*|\s*,\s+"):
            name = party_name(part).name
            if name and not _UTILITY_RE.search(name) and name not in out:
                out.append(name)
    return out


def _names_org(text: str, org: str, places: tuple[str, ...] = ()) -> bool:
    """Whether the text names the organization: its name without corporate suffixes, or its
    first word when that is distinctive ("TeraWulf", "Compass", "Aligned") and not one of the
    row's places ("Monticello" of Monticello Tech), written as a name."""
    bare = clean_text(_ORG_SUFFIX_RE.sub(" ", org)).strip(" ,.")
    if len(bare) >= 3 and re.search(rf"(?<!\w){re.escape(bare)}(?!\w)", text, re.I):
        return True
    first = bare.split()[0] if bare.split() else ""
    place_words = {w.casefold() for p in places for w in p.split()}
    return (
        len(first) >= 4
        and first.casefold() not in _GENERIC_WORDS
        and first.casefold() not in place_words
        and bool(re.search(rf"(?<!\w){re.escape(first)}(?!\w)", text))
    )


def _place_names(project: dict[str, Any]) -> list[str]:
    """The places the locality names: the place, the parenthesis and the counties ("Lansing",
    "Tompkins County"), and each part of a compound place."""
    loc = parse_locality(clean_text(project.get("locality")), clean_text(project.get("state")))
    names: list[str] = []
    for text in (loc.place, loc.hint, *loc.county_texts):
        for part in re.split(r"\s+/\s+|\s*&\s*|\s+and\s+", text or ""):
            part = re.sub(
                r"\s+(?:Townships?|Boroughs?|Village|City)$", "", part.strip(), flags=re.I
            )
            if len(part) >= 3 and part not in names:
                names.append(part)
    return names


def _site_words(project: dict[str, Any]) -> list[str]:
    """Words of the row's name that name the site, not its place, its company or a data center
    ("Starpointe", "Boberg", "Hoenert", "Plaza 500"): capitalized or numbered, not generic."""
    orgs = " ".join(_row_orgs(project)).casefold().split()
    loc = parse_locality(clean_text(project.get("locality")), clean_text(project.get("state")))
    places = " ".join([loc.place or "", *loc.county_texts]).casefold().split()
    words: list[str] = []
    name = clean_text(project.get("name"))
    for m in re.finditer(r"[A-Z][\w'-]*[a-z][\w'-]*(?:\s+\d+)?|\b\d{3,}\b", name):
        word = m.group(0)
        key = word.casefold()
        if (
            len(word) >= 4
            and key.split()[0] not in _GENERIC_WORDS
            and key.split()[0] not in orgs
            and key.split()[0] not in places
            and word not in words
        ):
            words.append(word)
    return words


def _acres_in(text: str) -> list[float]:
    return [float(m.group(1).replace(",", "")) for m in _ACRES_RE.finditer(text)]


def _same_acres(a: float, b: float) -> bool:
    """Two acreages of one site: within 10% (1,117 and 1,178 acres in West Memphis), or 1 acre."""
    return abs(a - b) <= max(1.0, 0.10 * max(a, b))


def source_date(url: object) -> date | None:
    """The publication day a link's path gives ("/2026/06/26/", "/2026/aug/07/", "2026-03-23"),
    if any."""
    path = urlsplit(clean_text(url)).path
    for m in _URL_DATE_RE.finditer(path):
        year, month, day = m.group("y"), m.group("m"), m.group("d")
        number = (
            int(month)
            if month.isdigit()
            else [n[:3] for n in _MONTH_NAMES].index(month[:3].casefold()) + 1
        )
        try:
            return date(int(year), number, int(day))
        except ValueError:
            continue
    return None


ReportHow = Literal["name", "site", "acres", "size", "org_place"]


def _names_a_place(project: dict[str, Any], text: str) -> bool:
    """Whether the text names one of the places the row's locality names."""
    return any(
        re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text, re.I) for p in _place_names(project)
    )


def names_project(project: dict[str, Any], summary: str) -> ReportHow | None:
    """How an entry names the row's project: its name, a site word of the name, its acreage, its
    size_mw with a data center ("the 24 MW data center"), or one of its organizations with its
    place or a data center ("TeraWulf ... the Lansing site", "the Prime Group data center
    project"). None when it does not."""
    folded = summary.casefold()
    name = _project_name(project)
    if len(name) > 3 and name.casefold() in folded:
        return "name"
    # A site word or the acreage names the land; with a data center, a campus or a project
    # named it names the project, unless the entry is about another use of the land (Hexa's
    # warehouse proposal before its data center; Monroe Township's redevelopment area of 2019).
    project_words = _mentions_data_center(project, summary) or (
        bool(_PROJECT_WORDS_RE.search(summary)) and not _OTHER_USE_RE.search(summary)
    )
    if project_words and any(
        re.search(rf"(?<!\w){re.escape(w)}(?!\w)", summary, re.I) for w in _site_words(project)
    ):
        return "site"
    acres = parse_number(project.get("acres"))
    if project_words and acres and any(_same_acres(a, acres) for a in _acres_in(summary)):
        return "acres"
    size = parse_number(project.get("size_mw"))
    if (
        size
        and _REPORT_SUBJECT_RE.search(summary)
        and any(
            abs(
                float(m.group(1).replace(",", "")) * (1000.0 if m.group(2)[0] in "Gg" else 1.0)
                - size
            )
            <= 0.5
            for m in _FIGURE_RE.finditer(summary)
        )
    ):
        return "size"
    places = tuple(_place_names(project))
    if any(_names_org(summary, o, places) for o in _row_orgs(project)) and (
        any(re.search(rf"(?<!\w){re.escape(p)}(?!\w)", summary, re.I) for p in places)
        or _mentions_data_center(project, summary)
    ):
        return "org_place"
    return None


@dataclass(frozen=True)
class FirstReport:
    """What the event log says of the project's first report: the first_reported event to
    import, if one can be told; else why not (doubt, with the entry it is about); and the earliest
    date the row shows the project was public (an entry naming it, or a milestone it states)."""

    event: dict[str, Any] | None
    doubt: str | None = None
    entry: dict[str, Any] | None = None
    earliest: AgwDate | None = None


def _mentions_data_center(project: dict[str, Any], summary: str) -> bool:
    """A data center named in the summary, not only inside a company name ("Aligned Data
    Centers")."""
    text = summary
    for org in _row_orgs(project):
        text = re.sub(re.escape(org), " ", text, flags=re.I)
    return bool(_REPORT_SUBJECT_RE.search(text))


def stated_milestones(project: dict[str, Any], today: date) -> list[tuple[AgwDate, str]]:
    """Every milestone date the row states, imported or not, with where it is stated:
    rezoning_filed, a past hearing_date (held or not: it was set, so the project was public),
    decided_date, and a filing month its note or an undated filing entry gives ("filed a
    conditional-use application in May 2026"; "the note")."""
    out = [
        (d, key)
        for key in ("rezoning_filed", "hearing_date", "decided_date")
        if (d := milestone_date(project, key)) is not None
    ]
    texts = [clean_text(project.get("note"))] + [
        clean_text(e.get("summary"))
        for e in row_events(project)
        if parse_day(e.get("date")) is None and own_filing(e)
    ]
    for text in texts:
        for m in _FILED_IN_RE.finditer(text):
            month = [n[:3] for n in _MONTH_NAMES].index(m.group("mon")[:3].casefold()) + 1
            out.append((AgwDate(date(int(m.group("year")), month, 1), "month"), "the note"))
    return [(d, where) for d, where in out if d.start <= today]


def first_report(
    project: dict[str, Any], status: Status, events: list[dict[str, Any]], today: date
) -> FirstReport:
    """The first report of a row without an announced date, from its event log.

    The entries are read in date order. An entry about the project names it (names_project: its
    name, a site word, its acreage, or an organization of the row with its place). Before it, an
    entry is passed over when it is the site's history or the place's rules and does not name the
    project: a property deal that states no data center plan (Plaza 500's purchase in 2022), an
    LLC's registration, an ordinance or a moratorium, another site of the same company (other
    acreage: Compass's first Red Oak campus of 225 acres, and after it an entry that names only the
    company and the place). The first entry about the project dates
    the report, at the entry's date or its source's publication day if earlier ("never later than
    the report"). There is no first_reported event, and `doubt` says why, when the earliest report
    cannot be told: an entry that only mentions a data center there; one about the place's rules
    that names the project (Hanover Township's ordinance, "before Prime ... submitted its
    Starpointe application"); one dated by the event it announces whose source gives no date
    (Mason County's "scheduled public hearings for March 25-26"). Nor is there one when the entry
    is not earlier
    than every milestone the row states (an unconfirmed hearing too: Wolcott's 2025-08-11
    hearing, ten months before its open house), when the stage is built or being built, when the
    entry is a construction or operation report, or when it would make a backward move. Its
    status is announced (proposed for one of the row's own filings), so it never sets the
    current status. `earliest` is the earliest date the row shows the project public."""
    milestones = stated_milestones(project, today)
    earliest, stated_in = min(milestones, key=lambda t: t[0].start) if milestones else (None, None)

    def filed_month(seen: AgwDate | None = None) -> FirstReport:
        """No entry dates the report before the milestones, and the earliest is a filing month
        only the note states (not imported): that month is the first report, as proposed."""
        if earliest is None or stated_in != "the note":
            return result(None, seen=seen)
        actual = [ev for ev in events if not ev["planned"]]
        if any(
            period_start(FuzzyDate.model_validate(ev["as_of"])) <= earliest.start for ev in actual
        ):
            return result(None, seen=seen)
        if any(ev["status"] == "announced" for ev in actual):
            return result(None, seen=seen)
        event = {
            "seq": 0,
            "status": "proposed",
            "event": "first_reported",
            "as_of": earliest.fuzzy(),
            "planned": False,
            "source_ids": ["s1"],
            "note": "The filing month AI GridWatch's row states (no earlier report in its event log)",
        }
        return result(event, seen=seen)

    def result(
        event: dict[str, Any] | None,
        doubt: str | None = None,
        entry: dict[str, Any] | None = None,
        seen: AgwDate | None = None,
    ) -> FirstReport:
        first = earliest
        if seen is not None and (first is None or seen.start < first.start):
            first = seen
        return FirstReport(event, doubt, entry, first)

    if status not in _REPORTED_STATUSES:
        return result(None)
    # With an announced milestone, the entries before it are still read (n31): one that reports
    # the project earlier dates the first report (Andover's township officials discussed it in
    # August 2025, before its December registration).
    announced = [
        period_start(FuzzyDate.model_validate(ev["as_of"]))
        for ev in events
        if ev["event"] == "announced" and not ev["planned"]
    ]
    bound = min(announced) if announced else None
    entries = sorted(
        (
            (day, i, e)
            for i, e in enumerate(dating_events(project))
            if (day := entry_date(e)) is not None and day.start <= today
        ),
        key=lambda t: (t[0].start, t[1]),
    )
    other_site = False
    doubt: tuple[str, dict[str, Any]] | None = None
    acres = parse_number(project.get("acres"))
    for day, _, e in entries:
        if bound is not None and day.end >= bound:
            break  # the announcement is the first report from here on
        summary = clean_text(e.get("summary"))
        kind = clean_text(e.get("kind")).casefold()
        how = names_project(project, summary)
        stated = _acres_in(summary)
        if (
            how is None
            and acres
            and _ENV_REVIEW_RE.search(summary)
            and any(_same_acres(a, acres) for a in stated)
        ):
            how = "acres"  # an environmental review of the site (n38: Monticello's AUAR)
        elsewhere = bool(_OTHER_SITE_RE.search(summary)) or bool(
            acres and stated and not any(_same_acres(a, acres) for a in stated)
        )
        if _REGISTRATION_RE.search(summary) or kind in _REPORT_SKIPPED_KINDS:
            continue  # an LLC's registration; the site as it stands or another facility's works
        if how is None:
            if _REPORT_CONTEXT_RE.search(summary) or not _mentions_data_center(project, summary):
                continue  # the place's rules or the site's history
            doubt = doubt or (
                "an entry mentions a data center there without naming the project, so it cannot "
                "be told whether it reports this one",
                e,
            )
            continue
        if how == "org_place" and elsewhere:
            other_site = True
            continue  # another site of the same company
        if _PROPERTY_RE.search(summary) and not _PLAN_RE.search(summary):
            continue  # a land deal dates the land, not the project
        if _PRIVATE_RE.search(summary):
            continue  # an act out of public view, revealed later (OpenAI's NDA to Effingham County)
        if bound is not None and how == "org_place" and not _names_a_place(project, summary):
            continue  # before AI GridWatch's announced date, the company alone is not the project
        published = source_date(e.get("source"))
        if bound is not None and published is not None and published >= bound:
            continue  # known only from a report of the announcement or after it
        if how == "org_place" and other_site:
            continue  # the company and the place only: taken as the other site's
        naming = [c for c in _CLAUSE_RE.split(summary) if names_project(project, c)] or [summary]
        if kind in _RULE_KINDS and all(_REPORT_CONTEXT_RE.search(c) for c in naming):
            continue  # about the rule itself (Hanover's ordinance, "before Prime ... submitted")
        if _SCHEDULED_RE.search(summary) and (published is None or published > day.end):
            reason = (
                "the earliest entry that names the project is dated by the event it announces, "
                "and its source gives no earlier date, so the report's date is not known"
            )
            return result(None, *(doubt or (reason, e)), seen=day)
        when = day
        if published is not None and published < day.start:
            when = AgwDate(published, "day")
        if doubt is not None:
            return result(None, *doubt, seen=when)
        if earliest is not None and when.start >= earliest.start:
            return filed_month(when)
        actual = [ev for ev in events if not ev["planned"]]
        own = (kind in _FILING_EVENT_KINDS and own_filing(e)) or kind == "permit"
        reported: Status = "proposed" if own else "announced"
        if reported == "proposed" and any(ev["status"] == "announced" for ev in actual):
            return result(None, seen=when)
        dated_by = " (dated by its source)" if when is not day else ""
        event = {
            "seq": 0,
            "status": reported,
            "event": "first_reported",
            "as_of": when.fuzzy(),
            "planned": False,
            "source_ids": ["s1"],
            "note": (
                f"Earliest report in AI GridWatch's event log ({kind[:40] or 'no kind'}){dated_by}"
            ),
        }
        return result(event, seen=when)
    return result(None, *doubt) if doubt else filed_month()


@dataclass(frozen=True)
class LogMilestone:
    """A milestone the row's event log or note reports that the stage has not reached, or an
    obstacle that leaves no application under review (status None for an application refused as
    incomplete, "paused" for a moratorium or ban adopted after the filing)."""

    status: Status | None
    what: str  # "an approval of its land-use application", "a groundbreaking", ...
    day: AgwDate | None  # None: the row's note says it
    kind: str
    source: str | None
    detail: str = ""  # appended to the reason (", with no filing after it")

    def where(self) -> str:
        """ "on 2026-04-01 (event kind 'vote')", or "in its note"."""
        if self.day is None:
            return "in its note"
        return f"on {self.day.fuzzy()['value']} (event kind {self.kind or 'blank'!r})"


def _unhedged(text: str, start: int, end: int, after: int = 0) -> bool:
    return not _LOG_HEDGE_RE.search(text[max(0, start - 40) : end + after])


def _log_entries(project: dict[str, Any]) -> list[tuple[AgwDate | None, dict[str, Any]]]:
    """The row's event-log entries with their dates, then its note (no date, kind "note")."""
    entries: list[tuple[AgwDate | None, dict[str, Any]]] = [
        (entry_date(e), e) for e in row_events(project)
    ]
    note = clean_text(project.get("note"))
    if note:
        entries.append((None, {"kind": "note", "summary": note, "source": project.get("source")}))
    return entries


# In the row's own note, the company, the developer or the project is the row's (n39: Nebius
# Independence's "Council approved Chapter 100 abatement 5-2 and the company has broken ground").
_NOTE_SUBJECT_RE = re.compile(
    r"\b(?:the (?:company|developer|applicant|project|owner)|it|they)\b", re.I
)


def _about_the_site(project: dict[str, Any], clause: str) -> bool:
    """Whether a clause is about the data center: it names a data center, a campus, the row's
    name or one of its organizations."""
    if _REPORT_SUBJECT_RE.search(clause) or re.search(r"\bcampus\b", clause, re.I):
        return True
    folded = clause.casefold()
    name = _project_name(project).casefold()
    return (len(name) > 3 and name in folded) or any(
        _names_org(clause, org) for org in _row_orgs(project)
    )


def log_milestone(project: dict[str, Any], status: Status, today: date) -> LogMilestone | None:
    """A milestone the event log or the note reports that an active stage has not reached: a
    denial of the project's land-use application with no filing after it (Forsyth County "voted
    5-2 to deny the rezoning request" while the stage still read Awaiting decision), else the
    most advanced of an approval of its rezoning, conditional or special use, special exception,
    site plan, development plan or PUD ("Person County commissioners voted to approve the
    rezoning"), one that a lawsuit seeks to overturn (Wolcott: a suit "to overturn the
    rezoning"), and a groundbreaking or construction under way that is not the power supply's
    (Meta Lebanon's note: "groundbreaking construction reported ongoing"; its log: construction
    disruption "during Meta campus build-out"). The kinds alone do not say it (a `vote` may go
    either way, a `construction` entry may be another site's), so only these phrases count, and a
    word close before them that defers, conditions or denies them voids them. The note is read
    for all but the denial (which needs the date of the filing after it)."""
    if status not in ACTIVE_ORDER:
        return None
    events = _log_entries(project)
    best: LogMilestone | None = None
    for day, e in events:
        if day is not None and day.start > today:
            continue
        source = clean_text(e.get("source")) or None
        kind = clean_text(e.get("kind"))
        refiled = day is None or any(
            later is not None and later.start > day.start and own_filing(other)
            for later, other in events
        )
        for clause in _CLAUSE_RE.split(clean_text(e.get("summary"))):
            if not refiled and any(
                _unhedged(clause, m.start(), m.start()) for m in _LOG_DENIAL_RE.finditer(clause)
            ):
                what = "a denial of its land-use application"
                return LogMilestone("denied", what, day, kind, source, ", and no filing after it")
            found: list[tuple[Status, str]] = []
            if any(
                _unhedged(clause, m.start(), m.start()) for m in _LOG_APPROVAL_RE.finditer(clause)
            ):
                found.append(("permitted", "an approval of its land-use application"))
            if any(
                _unhedged(clause, m.start(), m.start()) for m in _LOG_CHALLENGED_RE.finditer(clause)
            ):
                found.append(
                    ("permitted", "an approval of its land-use application, under court challenge")
                )
            if (
                not _LOG_ENERGY_RE.search(clause)
                and (
                    _about_the_site(project, clause)
                    or (day is None and bool(_NOTE_SUBJECT_RE.search(clause)))
                )
                and any(
                    _unhedged(clause, m.start(), m.end(), 20)
                    for m in _LOG_GROUNDBREAKING_RE.finditer(clause)
                )
            ):
                found.append(("under_construction", "a groundbreaking or construction under way"))
            for reported, what in found:
                rank = ACTIVE_ORDER.index(reported)
                if rank > ACTIVE_ORDER.index(status) and (
                    best is None or best.status is None or rank > ACTIVE_ORDER.index(best.status)
                ):
                    best = LogMilestone(reported, what, day, kind, source)
    return best


def filing_day(project: dict[str, Any]) -> AgwDate | None:
    """The row's first filing: rezoning_filed (as the row explains it, rezoning_filing), else its
    first own filing entry (own_filing)."""
    days = [d for d in [rezoning_filing(project).day] if d is not None] + [
        d for e in row_events(project) if own_filing(e) and (d := entry_date(e)) is not None
    ]
    return min(days, key=lambda d: d.start) if days else None


def _halted(project: dict[str, Any], today: date) -> LogMilestone | None:
    """An injunction or a halt of the site's works that no later entry says was lifted or ended."""
    found: LogMilestone | None = None
    for day, e in sorted(
        ((d, e) for d, e in _log_entries(project) if d is not None and d.start <= today),
        key=lambda t: t[0].start,
    ):
        summary = clean_text(e.get("summary"))
        if _LOG_RESUMED_RE.search(summary):
            found = None  # lifted, dissolved or resumed (after it, or in the same entry)
            continue
        for clause in _CLAUSE_RE.split(summary):
            if (
                not _LOG_ENERGY_RE.search(clause)
                and (
                    _about_the_site(project, clause)
                    or re.search(r"\b(?:site|property|project)\b", clause, re.I)
                )
                and any(
                    _unhedged(clause, m.start(), m.start())
                    and not _HALT_DENIED_RE.search(clause[max(0, m.start() - 40) : m.start()])
                    for m in _LOG_HALT_RE.finditer(clause)
                )
            ):
                what = "an injunction or a halt of its construction"
                found = LogMilestone(
                    "paused",
                    what,
                    day,
                    clean_text(e.get("kind")),
                    clean_text(e.get("source")) or None,
                )
                break
    return found


def log_obstacle(
    project: dict[str, Any], status: Status, today: date, public: date | None = None
) -> LogMilestone | None:
    """For a row at announced or proposed, an entry (or the note) that leaves no application under
    review although the stage implies one: its application refused as incomplete with the refusal
    upheld (Urbana: the BZA "unanimously denies Thor's appeal, upholding city determination that
    site plan application was incomplete"), or, after its filing, a moratorium or ban on data
    centers adopted that does not exempt it (Urbana's council "passed a 12-month moratorium on new
    data centers" three weeks after the filing). For a row at announced with no filing, a ban or
    moratorium adopted after the project was public (`public`: the earliest date the row shows;
    when it shows none, any) holds it too (n34: Andover's Ordinance 2026-13 banning data centers
    township-wide). For a row under construction, an injunction or a halt of its works that no
    later entry lifts (n34: Matrix's temporary injunction freezing construction). None
    otherwise."""
    if status == "under_construction":
        return _halted(project, today)
    if status not in ("announced", "proposed"):
        return None
    filed = filing_day(project)
    since: date | None = filed.end if filed is not None else None
    after = "after its filing"
    if filed is None and status == "announced":
        since, after = public, "after the project was public"
    for day, e in _log_entries(project):
        if day is not None and day.start > today:
            continue
        source = clean_text(e.get("source")) or None
        kind = clean_text(e.get("kind"))
        for clause in _CLAUSE_RE.split(clean_text(e.get("summary"))):
            if _LOG_INCOMPLETE_RE.search(clause) and _LOG_UPHELD_RE.search(clause):
                what = "its application refused as incomplete, and the refusal upheld"
                return LogMilestone(None, what, day, kind, source)
            # For an announced row without a filing, the note's ban counts too: any ban in force
            # blocks a project that has not applied.
            if (
                (filed is not None or status == "announced")
                and (day is not None or filed is None)
                and (since is None or day is None or day.start > since)
                and not _LOG_EXEMPT_RE.search(clean_text(e.get("summary")))
                and any(
                    _unhedged(clause, m.start(), m.end())
                    and not _MORATORIUM_HEDGE_RE.search(clause[max(0, m.start() - 40) : m.start()])
                    for m in _LOG_MORATORIUM_RE.finditer(clause)
                )
                and not _moratorium_ended(project, day)
            ):
                what = f"a moratorium or ban on data centers adopted {after}"
                return LogMilestone("paused", what, day, kind, source)
    return None


# A moratorium that ended (Leavenworth County's "Commission votes 3-2 against extending
# moratorium; moratorium expires").
_MORATORIUM_ENDED_RE = re.compile(
    r"\bmoratori(?:um|a)\s+(?:has\s+|had\s+)?(?:expir\w*|ended|ends|lapsed?)\b|\bwhich\s+expired\b|"
    r"\b(?:lift\w*|repeal\w*|rescind\w*|end\w*)\s+(?:the\s+|its\s+)?(?:[\w-]+\s+){0,2}?"
    r"(?:moratori(?:um|a)|ban)\b|\bagainst extending\b",
    re.I,
)


def _moratorium_ended(project: dict[str, Any], day: AgwDate | None) -> bool:
    """Whether a later entry (or, for the note's ban, the note) says the moratorium ended."""
    if day is None:
        return bool(_MORATORIUM_ENDED_RE.search(clean_text(project.get("note"))))
    return any(
        (d := entry_date(e)) is not None
        and d.start > day.start
        and _MORATORIUM_ENDED_RE.search(clean_text(e.get("summary")))
        for e in row_events(project)
    )


def withdrawal_reason(project: dict[str, Any]) -> StatusReason | None:
    """Why a Withdrawn row ended, from its note and its withdrawal entries: litigation when they
    cite a court ruling, developer_withdrawal when they name the developer, the applicant or a
    party of the row as the one who withdrew; else none (a mayor dropping his support, a lapsed
    agreement or a landowner backing out is not the developer's withdrawal)."""
    texts = [clean_text(project.get("note"))] + [
        clean_text(e.get("summary"))
        for e in row_events(project)
        if clean_text(e.get("kind")).casefold() in _WITHDRAWAL_EVENT_KINDS
    ]
    if any(_COURT_RULING_RE.search(t) for t in texts):
        return "litigation"
    names: set[str] = set()
    for role in ("operator", "owner", "filing_llc"):
        for part in split_names(clean_text(project.get(role)), r"\s+/\s+|\s*;\s*"):
            m = _TRAILING_NOTE_RE.match(part)
            # Only matched against the row's own text, never stored: a person's name may stay.
            for name in (m.group("name"), m.group("note")) if m else (part,):
                if name:
                    names.add(name)
                    first = name.split()[0]
                    if len(first) >= 3 and first[0].isupper():
                        names.add(first)
    who = "|".join(
        [r"developers?", r"applicants?", r"petitioners?", r"the company"]
        + [re.escape(n) for n in sorted(names, key=len, reverse=True)]
    )
    pattern = re.compile(rf"\b(?:{who})\b[^.;]{{0,60}}?{_WITHDREW_RE}", re.I)
    if any(pattern.search(t) for t in texts):
        return "developer_withdrawal"
    return None


def not_a_data_center(project: dict[str, Any]) -> str | None:
    """The phrase that makes the row a power supply deal or a generation facility rather than a
    data center site ("power purchase agreement", "solar farm", "not a data center"), if any."""
    for key in ("name", "note"):
        m = _NOT_A_DATA_CENTER_RE.search(clean_text(project.get(key)))
        if m:
            return m.group(0)
    return None


def _figure_basis(clause: str, m: re.Match[str]) -> SizeBasis | None:
    """The basis the words around a MW figure of a clause give it, if any."""
    before, after = clause[max(0, m.start() - 30) : m.start()], clause[m.end() : m.end() + 40]
    if _IT_AFTER_RE.match(after):
        return "it"
    if _POWER_AFTER_RE.match(after):
        return "power_source"
    if _SUPPLY_BEFORE_RE.search(before):
        return "utility_request"
    if _PHASE_BEFORE_RE.search(before) or _PHASE_AFTER_RE.match(after):
        return "phase"
    if _SUPPLY_AFTER_RE.match(after):
        return "utility_request"
    return None


def _figure_mw(m: re.Match[str]) -> float:
    unit = 1000.0 if m.group(2).casefold().startswith("g") else 1.0
    return float(m.group(1).replace(",", "")) * unit


def size_basis(size: float, note: str) -> SizeBasis | None:
    """What the row's note says size_mw measures: critical IT load ("up to 300MW critical IT"), a
    supply or interconnection ("Talen agreed to supply 960 MW", "3.2 GW contracted"), a power
    source's capacity ("835MW nuclear output", "450 MW gas-fired power plant") or one phase's
    ("phase 1 is 800MW"). None when the note does not give the figure or says nothing about it:
    AI GridWatch's schema calls the field "Planned IT/critical load", but its rows carry other
    bases, so the schema text alone is not the basis (07 §2.4)."""
    for clause in _CLAUSE_RE.split(clean_text(note)):
        for m in _FIGURE_RE.finditer(clause):
            if abs(_figure_mw(m) - size) > 0.5:
                continue
            return _figure_basis(clause, m)
    return None


def campus_acres(project: dict[str, Any]) -> tuple[float, str] | None:
    """(acres, the words) when the row's note or event log gives the row's named campus one
    acreage of its own ("the 304-acre BCG Cedar Creek Campus portion"); None when they give it
    none, or several."""
    name = _project_name(project)
    if len(name) < 4:
        return None
    pattern = re.compile(
        rf"(?<![\w.])(\d{{1,3}}(?:,\d{{3}})+|\d+(?:\.\d+)?)[- ]acres?\s+(?:the\s+)?"
        rf"{re.escape(name)}(?!\w)",
        re.I,
    )
    found: dict[float, str] = {}
    texts = [clean_text(project.get("note"))] + [
        clean_text(e.get("summary")) for e in row_events(project)
    ]
    for text in texts:
        for m in pattern.finditer(text):
            found.setdefault(float(m.group(1).replace(",", "")), m.group(0))
    if len(found) != 1:
        return None
    return next(iter(found.items()))


def party_replaced(project: dict[str, Any], name: str) -> str | None:
    """The words of the row's note or event log that say the organization is being replaced as
    the developer (n34: Sulphur Springs' mayor "announces a new developer will take over the
    Matrix Data Center Campus, with original developer MSB Global being phased out"), if any."""
    bare = clean_text(_ORG_SUFFIX_RE.sub(" ", name)).strip(" ,.")
    words = bare.split()
    names = [" ".join(words[:k]) for k in range(len(words), 0, -1)]
    names = [n for n in names if len(n) >= 6 and (" " in n or n == bare)]
    if not names:
        return None
    org = "(?:" + "|".join(re.escape(n) for n in names) + ")"
    pattern = re.compile(
        rf"(?<!\w){org}(?!\w)[^.;]{{0,60}}?\b(?:(?:is|was|being|will be|to be|been|are|were)\s+"
        rf"(?:phased out|replaced)|phased out|no longer (?:the |its )?(?:developer|involved))\b"
        rf"|\b(?:replac(?:ed|es|ing)|tak(?:e|es|ing|en)\s+over\s+from|succeed(?:s|ed|ing)?)\s+"
        rf"(?:the\s+)?(?:original\s+)?(?:developer\s+)?{org}(?!\w)",
        re.I,
    )
    texts = [clean_text(project.get("note"))] + [
        clean_text(e.get("summary")) for e in row_events(project)
    ]
    for text in texts:
        for clause in _CLAUSE_RE.split(text):
            if pattern.search(clause):
                return clause[:200]
    return None


def other_basis_figures(project: dict[str, Any], size: float, basis: SizeBasis) -> list[str]:
    """Event-log entries that give the campus another MW figure on the same basis as size_mw (n2:
    DataBank Red Oak's note says "up to 300MW critical IT", its 2024-09-10 entry "240MW of
    critical IT power" of a 480 MW campus): "{date}: {figure} MW". An entry about another site of
    the company (_OTHER_SITE_RE) is not the campus's."""
    out: list[str] = []
    for e in row_events(project):
        summary = clean_text(e.get("summary"))
        if _OTHER_SITE_RE.search(summary):
            continue
        for clause in _CLAUSE_RE.split(summary):
            for m in _FIGURE_RE.finditer(clause):
                mw = _figure_mw(m)
                if abs(mw - size) > 0.5 and _figure_basis(clause, m) == basis:
                    out.append(f"{clean_text(e.get('date')) or 'no date'}: {mw:g} MW")
    return out


# A clause that gives the context of an entry's subject ("..., months after the county amended
# its zoning ordinance"), and the verbs of an applicant's filing.
_CONTEXT_CLAUSE_RE = re.compile(
    r",\s+(?:[\w-]+\s+){0,2}?(?:after|before|since|following|when|while|once|until)\b", re.I
)
_FILED_VERB_RE = re.compile(r"\b(?:submit(?:s|ted)?|filed|files|applied|applies)\b", re.I)


def own_filing(entry: dict[str, Any]) -> bool:
    """Whether an event-log entry is one of the row's own applications: of kind filing or
    rezoning, with a source that is not a placeholder link, about an application, a petition, a
    request, a plan or a permit, and not about the place's rules (an ordinance, a moratorium, a
    text amendment, a resolution, fees, ...), a property deal or someone else's lawsuit, appeal
    or motion. The place's rules may come in as the context of a filing: an entry whose first
    clause files an application and names no rule is one, whatever a later clause says of an
    ordinance (n15: Pronghorn "submitted a conditional use permit application in November 2025
    ..., months after the county amended its zoning ordinance")."""
    summary = clean_text(entry.get("summary"))
    if (
        clean_text(entry.get("kind")).casefold() not in _FILING_EVENT_KINDS
        or placeholder_source(entry)
        or not _APPLICATION_RE.search(summary)
        or _NOT_AN_APPLICATION_RE.search(summary)
    ):
        return False
    if not _REPORT_CONTEXT_RE.search(summary):
        return True
    subject = _CONTEXT_CLAUSE_RE.split(_CLAUSE_RE.split(summary)[0], maxsplit=1)[0]
    return (
        bool(_FILED_VERB_RE.search(subject))
        and bool(_APPLICATION_RE.search(subject))
        and not _REPORT_CONTEXT_RE.search(subject)
    )


def has_filing(project: dict[str, Any]) -> bool:
    """A rezoning_filed date the row shows is a filing (rezoning_filing), an announced date it
    explains as a filing, or an entry that is one of the row's own applications."""
    if rezoning_filing(project).filed:
        return True
    return any(own_filing(e) for e in row_events(project))


def no_application(project: dict[str, Any]) -> str | None:
    """The phrase of the row's note, or of an event-log entry about its project, that says nothing
    has been formally proposed, or the note's words that say only an inquiry was made, when no
    filing is known (has_filing): a stage that implies an application is then announced (n30:
    Project Zora's "no formal application or site review had been submitted"; n40)."""
    if has_filing(project):
        return None
    texts = [clean_text(project.get("note"))] + [
        summary
        for e in dating_events(project)
        if names_project(project, summary := clean_text(e.get("summary"))) is not None
    ]
    for text in texts:
        m = _NO_APPLICATION_RE.search(text)
        if m:
            return m.group(0)
    inquiry = _INQUIRY_RE.search(clean_text(project.get("note")))
    return inquiry.group(0) if inquiry and milestone_date(project, "rezoning_filed") else None


_TERMINAL_EVENTS = frozenset({"denied", "withdrawn", "cancelled", "paused"})


def _terminal_first(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The earliest dated event, when it is a denial, a withdrawal, a cancellation or a pause."""
    actual = [e for e in events if not e["planned"] and e["event"] != "other"]
    if not actual:
        return None
    first = min(actual, key=lambda e: period_start(FuzzyDate.model_validate(e["as_of"])))
    return first if first["event"] in _TERMINAL_EVENTS else None


def _latest_actual(events: list[StatusEvent]) -> StatusEvent | None:
    actual = [e for e in events if not e.planned]
    return max(actual, key=event_key) if actual else None


# ---------------------------------------------------------------------------- Epoch AI's sites


def _url_key(url: str) -> str | None:
    try:
        return str(HttpUrl(clean_text(url)))
    except ValidationError:
        return None


def row_links(project: dict[str, Any]) -> frozenset[str]:
    """The row's `source` and every event's `source`, normalized."""
    events = project.get("events")
    urls = [project.get("source")] + [
        e.get("source") for e in (events if isinstance(events, list) else []) if isinstance(e, dict)
    ]
    return frozenset(k for u in urls if (text := clean_text(u)) and (k := _url_key(text)))


def name_slug(text: str) -> str:
    """A name in AI GridWatch's id form: accents dropped, lower case, every other run of
    characters one "-" ("Microsoft-Nebius New Jersey" -> "microsoft-nebius-new-jersey")."""
    folded = unicodedata.normalize("NFKD", clean_text(text))
    plain = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def _has_run(tokens: tuple[str, ...], run: tuple[str, ...]) -> bool:
    n = len(run)
    return n > 0 and any(tokens[i : i + n] == run for i in range(len(tokens) - n + 1))


def id_stems(project: dict[str, Any]) -> tuple[str, ...]:
    """The forms a row's id starts with: its name in id form, and for a generic name ("Unnamed
    Data Center") its operator's and the name's ("ksr-unnamed-data-center")."""
    name = clean_text(project.get("name"))
    stems = [name_slug(name)]
    operator = clean_text(project.get("operator"))
    if _GENERIC_NAME_RE.match(name) and operator:
        stems.append(f"{name_slug(operator)}-{name_slug(name)}")
    return tuple(s for s in stems if s)


def _starts_with(pid: str, stem: str) -> bool:
    return pid == stem or pid.startswith(stem + "-")


def foreign_ids(projects: list[Any]) -> dict[str, str]:
    """Rows whose id starts with another row's name (in the same state) and not with their own:
    {row id: the id of the row it names}. On 2026-10-08 one block of PA DEP rows had each row's id
    on the next row ("zediker-station-…" holding PECO's Limerick project, the Zediker Station row
    under "highlands-west-…"). Records are matched on the id, so such a row is held: imported
    under the id, it would name another project and churn once AI GridWatch fixes the ids. A stem
    of one word ("aws") is too common to say whose id it is."""
    rows: list[tuple[str, str, tuple[str, ...]]] = []
    by_state: dict[str, list[tuple[str, str]]] = {}
    for project in projects:
        if not isinstance(project, dict) or not (pid := clean_text(project.get("id"))):
            continue
        state = clean_text(project.get("state")).upper()
        stems = id_stems(project)
        rows.append((pid, state, stems))
        by_state.setdefault(state, []).extend((s, pid) for s in stems if "-" in s)
    out: dict[str, str] = {}
    for pid, state, stems in rows:
        if any(_starts_with(pid, s) for s in stems):
            continue
        other = next(
            (o for s, o in by_state.get(state, []) if o != pid and _starts_with(pid, s)), None
        )
        if other is not None:
            out[pid] = other
    return out


def org_key(name: str) -> str:
    """An organization name for comparison: accents, corporate suffixes and everything but
    letters and digits dropped ("X.AI Corp" -> "xai", "SpaceXAI" -> "spacexai")."""
    folded = unicodedata.normalize("NFKD", clean_text(name))
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", _ORG_SUFFIX_RE.sub(" ", plain).casefold())


def orgs_overlap(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two sets of org_key names may share an organization: the same key, or one key of
    three or more characters inside the other (xAI in SpaceXAI, QTS in QTS Data Centers). With
    data/orgs.json empty in M1 there is no alias table, and the rules that read this only hold a
    row for review, so a near match counts rather than a merge on a name that differs."""
    return any(
        x == y or (min(len(x), len(y)) >= 3 and (x in y or y in x)) for x in a for y in b if x and y
    )


def record_orgs(record: FacilityRecord) -> frozenset[str]:
    parties = record.parties
    return frozenset(
        k
        for o in (*parties.operator, *parties.owner, *parties.developer, *parties.tenant)
        if (k := org_key(o.name))
    )


@dataclass(frozen=True)
class Twin:
    """How an AI GridWatch row is the same site as a stored Epoch AI record.

    record_id is the Epoch record. It is None for "epoch_source", and for "weak_link", "nearby"
    and "several", where candidates lists the Epoch records the row may be: those rows are held
    for review, not merged."""

    how: str
    record_id: str | None
    candidates: tuple[str, ...] = ()
    evidence: str | None = None  # the link or street that tied the row to the record
    detail: str | None = None  # why a link is too weak to merge on ("weak_link")


@dataclass(frozen=True)
class EpochSite:
    """What the location rules read from one stored Epoch AI record."""

    record_id: str
    name: str  # the first epoch_name
    state: str
    county_fips: str | None  # the stated county, else the county containing the point
    lat: float | None
    lon: float | None
    precise: bool  # placed at locality precision or finer
    orgs: frozenset[str]  # owner, operator, developer and tenant names, as org_key
    street: tuple[str, ...]  # house number and street words (street_tokens), () when none
    mw: float | None = None  # IT MW, else facility MW (mw_display)
    # The city and municipality the record states (an override's), base names normalized.
    places: frozenset[str] = frozenset()


@dataclass
class EpochSites:
    """The stored Epoch AI records, by state and name and by the links they cite, plus the AI
    GridWatch rows that copy them (see `twin`).

    AI GridWatch carries most of Epoch's US sites under Epoch's names, with `source` set to the
    first of Epoch's Selected Sources for the site (68 of the 69 same-name rows on 2026-10-08).
    For some of those sites it also has a row of its own under another name.
    """

    by_name: dict[tuple[str, str], str] = field(default_factory=dict)
    by_slug: dict[tuple[str, str], str] = field(default_factory=dict)
    # Each cited link (normalized) -> the (state, record id) of every Epoch record citing it.
    by_url: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    sites: dict[str, EpochSite] = field(default_factory=dict)
    counties: CountyIndex | None = None
    # AI GridWatch rows (add_rows): each link -> the ids of the rows citing it, and the rows that
    # are an Epoch site's copy (matched by name, id or source) -> that Epoch record.
    rows_by_link: dict[str, set[str]] = field(default_factory=dict)
    copies: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_records(
        cls, records: Mapping[str, FacilityRecord], counties: CountyIndex | None = None
    ) -> EpochSites:
        from atlas.geocode import house_number, street_name_tokens

        sites = cls(counties=counties)
        for rid, r in sorted(records.items()):
            names = r.external_ids.get("epoch_name") or []
            if not names or r.merged_into:
                continue
            state = r.location.state_abbr
            for n in names:
                sites.by_name.setdefault((state, clean_text(n).casefold()), rid)
                if slug := name_slug(n):
                    sites.by_slug.setdefault((state, slug), rid)
            for src in r.sources:
                key = _url_key(str(src.url)) if src.source_type != "open_dataset" else None
                cited = sites.by_url.setdefault(key, []) if key else None
                if cited is not None and (state, rid) not in cited:
                    cited.append((state, rid))
            loc = r.location
            house = house_number(loc.street)
            street_words = street_name_tokens(loc.street) if house else ()
            sites.sites[rid] = EpochSite(
                record_id=rid,
                name=clean_text(names[0]),
                state=state,
                county_fips=loc.county_fips or sites._county_at(loc.lat, loc.lon),
                lat=loc.lat,
                lon=loc.lon,
                precise=loc.precision in _PRECISE,
                orgs=record_orgs(r),
                street=(house, *street_words) if house and street_words else (),
                mw=mw_display(r.capacity)[0],
                places=frozenset(_place_key(p) for p in (loc.city, loc.municipality) if p),
            )
        return sites

    def _county_at(self, lat: float | None, lon: float | None) -> str | None:
        if self.counties is None or lat is None or lon is None:
            return None
        county = self.counties.lookup(lat, lon)
        return county.fips if county else None

    def match(self, project: dict[str, Any], state: str) -> tuple[str, str | None] | None:
        """(how, Epoch record id) when the row is the same site as an Epoch record: "name" (the
        same name in the same state), "id" (its id is an Epoch name in AI GridWatch's id form,
        with or without "-{state}", in the same state), "source" (its source is a link that
        exactly one Epoch record cites, in the same state; a report cited for several sites links
        none) or "epoch_source" (its source is an Epoch AI page; record id None)."""
        rid = self.by_name.get((state, clean_text(project.get("name")).casefold()))
        if rid is not None:
            return ("name", rid)
        pid = clean_text(project.get("id"))
        rid = self.by_slug.get((state, pid)) or self.by_slug.get(
            (state, pid.removesuffix(f"-{state.lower()}"))
        )
        if rid is not None:
            return ("id", rid)
        source = clean_text(project.get("source"))
        key = _url_key(source) if source else None
        cited = self.by_url.get(key, []) if key else []
        if len(cited) == 1 and cited[0][0] == state:
            return ("source", cited[0][1])
        host = host_of(source) if key else ""
        if host == _EPOCH_HOST or host.endswith("." + _EPOCH_HOST):
            return ("epoch_source", None)
        return None

    def add_rows(self, projects: list[Any]) -> None:
        """Index every row's links, and the verified rows that `match` ties to an Epoch record
        (AI GridWatch's copies of Epoch's sites, whose event logs AI GridWatch wrote itself)."""
        for project in projects:
            if not isinstance(project, dict) or not clean_text(project.get("id")):
                continue
            pid = clean_text(project["id"])
            for key in row_links(project):
                self.rows_by_link.setdefault(key, set()).add(pid)
            state = clean_text(project.get("state")).upper()
            if project.get("verified") is True and state in IN_SCOPE:
                direct = self.match(project, state)
                if direct is not None and direct[1] is not None:
                    self.copies[pid] = direct[1]

    def _site_of_link(self, key: str, pid: str) -> str | None:
        """The one Epoch site a link is specific to: cited by that site's record or by AI
        GridWatch's copy of it, by no other Epoch record, and by no other AI GridWatch row."""
        owners = {rid for _, rid in self.by_url.get(key, [])}
        for other in self.rows_by_link.get(key, set()) - {pid}:
            if other not in self.copies:
                return None
            owners.add(self.copies[other])
        return next(iter(owners)) if len(owners) == 1 else None

    def _cites(self, rid: str, key: str | None) -> bool:
        """Whether the Epoch record, or an AI GridWatch copy of its site, cites the link."""
        if key is None:
            return False
        if any(r == rid for _, r in self.by_url.get(key, [])):
            return True
        return any(self.copies.get(other) == rid for other in self.rows_by_link.get(key, set()))

    def _weak_link(self, project: dict[str, Any], rid: str, evidence: str) -> str | None:
        """Why a link that ties the row to one site is too weak to merge on, if it is: the tie is
        only in the row's event log and the row's own source is cited by neither the Epoch record
        nor its copy (AI GridWatch copies a campus's history into rows about other projects: its
        Amazon Northern Indiana expansion carries the New Carlisle stories), or the two MW differ
        by more than x2 (07 §6.6, Size)."""
        source = clean_text(project.get("source"))
        key = _url_key(source) if source else None
        if evidence != key and not self._cites(rid, key):
            return "the link is only in the row's event log, and its own source is not the site's"
        site = self.sites.get(rid)
        size = parse_number(project.get("size_mw"))
        mw = site.mw if site is not None else None
        if mw and size and size > 0 and max(size, mw) / min(size, mw) > MW_FACTOR:
            return f"its {size:g} MW and the site's {mw:g} MW differ by more than x2"
        return None

    def _local_orgs(self, record: FacilityRecord, here: str | None) -> set[str]:
        """The Epoch sites in the county (`here`) that may share an organization with the row."""
        orgs = record_orgs(record)
        return {
            s.record_id
            for s in self.sites.values()
            if s.state == record.location.state_abbr
            and here is not None
            and s.county_fips == here
            and orgs_overlap(s.orgs, orgs)
        }

    def _nearby(self, record: FacilityRecord) -> tuple[str, ...]:
        """Epoch sites within 5 km that may share an organization, both points at locality
        precision or finer."""
        from atlas.geocode import distance_m

        loc = record.location
        orgs = record_orgs(record)
        if loc.precision not in _PRECISE or loc.lat is None or loc.lon is None or not orgs:
            return ()
        return tuple(
            sorted(
                s.record_id
                for s in self.sites.values()
                if s.state == loc.state_abbr
                and s.precise
                and orgs_overlap(s.orgs, orgs)
                and s.lat is not None
                and s.lon is not None
                and distance_m(loc.lat, loc.lon, s.lat, s.lon) <= NEARBY_M
            )
        )

    def twin(self, project: dict[str, Any], record: FacilityRecord) -> Twin | None:
        """The location rules, for a row that `match` does not tie to Epoch and that became
        `record`. The row lies in an Epoch site's county and either cites a link specific to that
        site ("link") or gives its street address in its name or id ("street").

        A link merges only when it is the row's own source (or the row's source is one the site
        or its copy cites) and the MW agree within x2; otherwise the row is held for review
        ("weak_link", the site in candidates). The row is also held, not merged ("several": the
        record ids are in candidates), when the rules point to more than one site, or when it may
        also share an organization with another Epoch site in that county (AI GridWatch sometimes
        lists a company's campuses in a county as one row; names are compared with orgs_overlap,
        so xAI and SpaceXAI count as one); and ("nearby") when it only may share an organization
        with Epoch sites within 5 km, both points at locality precision or finer.
        """
        from atlas.geocode import street_tokens

        loc = record.location
        pid = clean_text(project.get("id"))
        here = loc.county_fips or self._county_at(loc.lat, loc.lon)
        source = clean_text(project.get("source"))
        source_key = _url_key(source) if source else None
        local = [s for s in self.sites.values() if s.state == loc.state_abbr]
        found: dict[str, tuple[str, str]] = {}
        if here is not None:
            for key in sorted(row_links(project)):
                rid = self._site_of_link(key, pid)
                site = self.sites.get(rid) if rid else None
                if (
                    site is not None
                    and site.state == loc.state_abbr
                    and site.county_fips == here
                    and (site.record_id not in found or key == source_key)
                ):
                    found[site.record_id] = ("link", key)
            words = " ".join(_WORD_RE.findall(f"{clean_text(project.get('name'))} {pid}"))
            tokens = street_tokens(words)
            for site in local:
                if site.street and site.county_fips == here and _has_run(tokens, site.street):
                    found.setdefault(site.record_id, ("street", " ".join(site.street)))
        if found:
            also = self._local_orgs(record, here)
            if len(found) == 1 and not also - set(found):
                ((rid, (how, evidence)),) = found.items()
                weak = self._weak_link(project, rid, evidence) if how == "link" else None
                if weak is not None:
                    return Twin("weak_link", None, (rid,), evidence=evidence, detail=weak)
                return Twin(how, rid, evidence=evidence)
            return Twin("several", None, tuple(sorted(set(found) | also)))
        same = self._same_place(project, record, here)
        if same:
            return Twin("place", None, same)
        near = self._nearby(record)
        return Twin("nearby", None, near) if near else None

    def _same_place(
        self, project: dict[str, Any], record: FacilityRecord, here: str | None
    ) -> tuple[str, ...]:
        """Epoch sites in the row's county whose record states the city or municipality the row's
        locality names, and that may share an organization with the row (AWS Salem Township and
        Epoch's AWS Berwick, which an override places in Salem Township at its county's point,
        so no distance rule can tie them)."""
        state = record.location.state_abbr
        loc = parse_locality(clean_text(project.get("locality")), state)
        named = {_place_key(name) for name, _ in place_parts(loc.place)} if loc.place else set()
        orgs = record_orgs(record)
        if not named or here is None or not orgs:
            return ()
        return tuple(
            sorted(
                s.record_id
                for s in self.sites.values()
                if s.state == state
                and s.county_fips == here
                and s.places & named
                and orgs_overlap(s.orgs, orgs)
            )
        )

    def copy_of_another(self, project: dict[str, Any], twin: Twin) -> Twin:
        """The twin, unless the row is AI GridWatch's copy of an Epoch site (its note quotes
        "Epoch AI estimates") whose name is a numbered sibling of every record the rule found
        ("QTS Richmond 2" against "QTS Richmond 1"): then the copy's own site has no record yet,
        and the rule tied it to its sibling (QTS Richmond 2 and 3, whose sources are QTS
        Richmond 1's pages). Such a row is held without naming a record ("epoch_copy")."""
        note = clean_text(project.get("note"))
        if twin.how in ("name", "id") or not _EPOCH_COPY_RE.search(note):
            return twin
        ids = {twin.record_id} if twin.record_id else set(twin.candidates)
        name = clean_text(project.get("name"))
        if ids and all(r in self.sites and _siblings(name, self.sites[r].name) for r in ids):
            return Twin("epoch_copy", None)
        return twin

    def candidates(self, project: dict[str, Any], record: FacilityRecord) -> tuple[str, ...]:
        """The Epoch records a row held for another reason (its source is an Epoch AI page) may
        be, for the reviewer: what the location rules tie it to, the sites in its county that may
        share an organization, and those within 5 km. Nothing is merged."""
        loc = record.location
        twin = self.twin(project, record)
        ids = set(twin.candidates) if twin else set()
        if twin is not None and twin.record_id is not None:
            ids.add(twin.record_id)
        ids |= self._local_orgs(record, loc.county_fips or self._county_at(loc.lat, loc.lon))
        ids |= set(self._nearby(record))
        return tuple(sorted(ids))

    def names(self, record_ids: tuple[str, ...]) -> str:
        """The records as "AWS Berwick (gwa-…)", for a review reason."""
        return ", ".join(
            f"{self.sites[r].name} ({r})" if r in self.sites else r for r in record_ids
        )


def stage_support(project: dict[str, Any], status: Status, today: date) -> dict[str, Any] | None:
    """The milestone, else the event-log entry, that supports a stage's status, if any: the
    latest milestone event with that status, or an approval or groundbreaking the event log
    reports (log_milestone) that is that status."""
    milestones = [
        e for e in milestone_events(project, today) if not e["planned"] and e["status"] == status
    ]
    if milestones:
        e = milestones[-1]
        return {"event": e["event"], "as_of": e["as_of"]["value"]}
    if status in ACTIVE_ORDER and status != ACTIVE_ORDER[0]:
        below = ACTIVE_ORDER[ACTIVE_ORDER.index(status) - 1]
        log = log_milestone(project, below, today)
        if log is not None and log.status == status:
            return {
                "event_log": log.what,
                "as_of": log.day.fuzzy()["value"] if log.day else None,
                "event_kind": log.kind or None,
                "event_source": log.source,
            }
    return None


# ---------------------------------------------------------------------------- reviewer releases


ReleaseCheck = Literal["epoch", "stage", "id", "scope", "first_report"]


class RowRelease(AtlasModel):
    """One entry of config/overrides/aigridwatch.json: a reviewer's decision that a check which
    holds an AI GridWatch row is wrong for it, so the row is imported.

    release names the checks: "epoch" (the Epoch AI site rules: the reviewer found the row is
    another site), "stage" (the event log's later milestone, or an obstacle, does not hold: the
    stage is right), "id" (the id that starts with another row's name), "scope" (a power supply
    deal or generation facility) and "first_report" (the row shows the project public before its
    earliest milestone: the reviewer accepts that milestone as the first report).
    as_of is the day the reviewer confirmed the stage of a row that has no as_of (the stage's
    observation is then noted as confirmed on that day instead of seen in the file).
    first_reported is the day the reviewer found the project first reported (the source's
    publication day), for a row whose event log does not tell it.
    reason says what the reviewer read; reviewed_at is when."""

    release: list[ReleaseCheck] = Field(default_factory=list)
    as_of: date | None = None
    first_reported: date | None = None
    reason: str = Field(min_length=1, max_length=300)
    reviewed_at: date

    @model_validator(mode="after")
    def _releases_something(self) -> RowRelease:
        if not self.release and self.as_of is None and self.first_reported is None:
            raise ValueError(
                "an entry releases at least one check or gives as_of or first_reported"
            )
        return self


_RELEASES = TypeAdapter(dict[str, RowRelease])


def load_releases(path: Path) -> dict[str, RowRelease]:
    """The reviewer releases. The default file may be absent (no release); a named one may not."""
    from atlas.geo.counties import resolve_reference

    resolved = resolve_reference(path)
    if path == OVERRIDES_PATH and not resolved.exists():
        return {}
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as e:
        raise agw_error(f"cannot read the AI GridWatch overrides file {resolved}: {e}") from e
    try:
        releases = _RELEASES.validate_json(text)
    except ValidationError as e:
        raise agw_error(f"{resolved} is not a valid AI GridWatch overrides file: {e}") from e
    for pid, entry in releases.items():
        if clean_text(pid) != pid or not pid or find_personal_data(entry.reason):
            raise agw_error(
                f"{resolved}: entry {pid!r} needs a plain row id and a reason without contact details"
            )
    return releases


# ---------------------------------------------------------------------------- the importer


class AIGridWatchImporter:
    name = "aigridwatch"
    match_key = "aigridwatch_id"
    owned_external_keys = ("aigridwatch_id",)
    review_sources = ("aigridwatch",)
    version = "4"
    help = "AI GridWatch project tracker (CC BY 4.0): contested proposals and their milestones"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--without-epoch",
            action="store_true",
            help="run on a store without Epoch AI records (only rows citing an Epoch AI page are "
            "held as Epoch's sites); without it the run refuses, since epoch must run first",
        )
        parser.add_argument(
            "--overrides",
            type=Path,
            default=OVERRIDES_PATH,
            help="reviewer releases of held rows (default: config/overrides/aigridwatch.json; "
            "none when that file is absent)",
        )

    def run(self, ctx: ImportContext, args: argparse.Namespace) -> ImportResult:
        releases = load_releases(getattr(args, "overrides", OVERRIDES_PATH))
        if not getattr(args, "without_epoch", False) and not any(
            r.external_ids.get("epoch_name") and not r.merged_into for r in ctx.records.values()
        ):
            raise agw_error(
                "the store holds no Epoch AI record. AI GridWatch republishes Epoch's sites, and "
                "only the Epoch records keep them from being imported twice: run `atlas import "
                "epoch` first, or pass --without-epoch"
            )

        def fetch_json() -> FetchResult:
            from atlas.net import fetch

            return fetch(
                ctx.http, PROJECTS_URL, allowed_types=("application/json",), max_bytes=20_000_000
            )

        data, snapshot = load_input(
            ctx, name=self.name, url=PROJECTS_URL, license=LICENSE, ext="json", fetch=fetch_json
        )
        try:
            doc = json.loads(data)
        except ValueError as e:
            raise agw_error(f"AI GridWatch projects.json is not JSON: {e}") from e
        if not isinstance(doc, dict) or not isinstance(doc.get("projects"), list):
            raise agw_error("AI GridWatch projects.json has no projects list; the layout changed")
        if doc.get("license") != UPSTREAM_LICENSE:
            raise agw_error(
                f"AI GridWatch license is {doc.get('license')!r}, not {UPSTREAM_LICENSE!r}; "
                "check the terms before importing"
            )
        generated = clean_text(doc.get("generated")) or None
        snapshot = snapshot.model_copy(update={"upstream_version": generated})
        generated_day = parse_day(generated) or ctx.today
        for pid, entry in releases.items():
            for key, value in (("as_of", entry.as_of), ("first_reported", entry.first_reported)):
                if value is not None and value > ctx.today:
                    raise agw_error(
                        f"config/overrides/aigridwatch.json entry {pid!r}: {key} is after today"
                    )

        gazetteer = ctx.gazetteer()  # with the county subdivisions (downloaded on first use)
        counties = ctx.counties()
        places = ctx.places()  # the place polygons, for the city of a row's own point
        areas = _load_cousub_areas(ctx)
        epoch_sites = EpochSites.from_records(ctx.records, counties)
        projects: list[Any] = doc["projects"]
        epoch_sites.add_rows(projects)
        foreign = foreign_ids(projects)
        row_ids = {clean_text(p.get("id")) for p in projects if isinstance(p, dict)}
        # AI GridWatch records already stored, not yet merged: one for a row now held is a twin.
        stored_agw = {
            v: rid
            for rid, r in ctx.records.items()
            if not r.merged_into
            for v in r.external_ids.get(self.match_key, [])
        }
        retrieved_at = snapshot.retrieved_at.isoformat()
        candidates: list[Candidate] = []
        review: list[ReviewItem] = []
        stats: Counter[str] = Counter()

        def flag(
            kind: str,
            external_id: str | None,
            reason: str,
            *,
            record_id: str | None = None,
            **data: Any,
        ) -> None:
            review.append(
                ReviewItem(
                    source=self.name,
                    kind=kind,
                    external_id=external_id,
                    record_id=record_id,
                    reason=reason,
                    data=data,
                )
            )

        def released(
            pid: str, release: RowRelease, check: str, kind: str, why: str, **data: Any
        ) -> None:
            """A check that would hold the row, released by a reviewer: noted, and imported."""
            stats["released"] += 1
            flag(
                kind,
                pid,
                f"{why}; released by config/overrides/aigridwatch.json (reviewed "
                f"{release.reviewed_at.isoformat()}: {release.reason}), so the row is imported",
                released=check,
                reviewed_at=release.reviewed_at.isoformat(),
                **data,
            )

        def epoch_why(twin: Twin) -> str:
            several = epoch_sites.names(twin.candidates)
            return {
                "name": "the same site as an Epoch AI record (same name and state)",
                "id": "the same site as an Epoch AI record (its id is that record's name, in "
                "the same state)",
                "source": "the same site as an Epoch AI record (it cites a link that record "
                "cites, in the same state)",
                "epoch_source": "AI GridWatch's source for this row is an Epoch AI page"
                + (f"; it may be Epoch AI's {several}" if twin.candidates else ""),
                "link": "the same site as an Epoch AI record (it lies in its county, and its own "
                "source is a link that site cites, or AI GridWatch's copy of it, and no other)",
                "street": "the same site as an Epoch AI record (it lies in its county and its "
                "name gives that record's street address)",
                "weak_link": f"may be Epoch AI's {several} (it lies in its county and cites a "
                f"link that only that site cites, but {twin.detail})",
                "nearby": f"may be the same site as Epoch AI's {several} (an organization in "
                "common, within 5 km)",
                "place": f"may be the same site as Epoch AI's {several} (an organization in "
                "common, and both name the same city or municipality in the same county)",
                "epoch_copy": "AI GridWatch's copy of an Epoch AI site of this name (its note "
                "quotes Epoch AI's estimate), which no stored Epoch record holds; a rule ties it "
                "to an Epoch record of another name, which is not this site",
                "several": f"may be one or more of Epoch AI's {several} (a rule ties it to one "
                "of them, and another rule or an organization in common to another in the same "
                "county)",
            }[twin.how]

        def held(pid: str, project: dict[str, Any], twin: Twin) -> None:
            stats["epoch_duplicates"] += 1
            why = epoch_why(twin)
            if twin.candidates:
                stats["epoch_ambiguous"] += 1
                why += (
                    "; held for review, not merged: the row is not imported, and a reviewer "
                    "decides which site it is"
                )
            elif twin.record_id is None:
                why += "; held, not imported: a reviewer decides which Epoch AI site it is"
            elif twin.how in ("name", "id", "source") and "Epoch AI" in clean_text(
                project.get("note")
            ):
                why += (
                    "; AI GridWatch republishes this Epoch AI site, so the row is not imported a "
                    "second time"
                )
            else:
                why += (
                    "; AI GridWatch's own row for this Epoch site: not imported, and nothing from "
                    "it is merged into the Epoch record (its milestones wait for M4 entity "
                    "resolution)"
                )
            extra: dict[str, Any] = {}
            if twin.candidates:
                extra["epoch_records"] = list(twin.candidates)
            if twin.evidence:
                extra["evidence"] = twin.evidence
            stale = stored_agw.get(pid)
            if stale is not None:
                # merge_import files removed_upstream for it; this item says why.
                extra["stored_record"] = stale
                why += (
                    f". The store already holds AI GridWatch record {stale} for this row: it is "
                    "kept (records are never deleted) until a reviewer sets its merged_into"
                )
            flag(
                "possible_duplicate",
                pid,
                why,
                record_id=twin.record_id,
                matched_by=twin.how,
                source=clean_text(project.get("source")) or None,
                **extra,
            )
            epoch = ctx.records.get(twin.record_id) if twin.record_id else None
            if epoch is not None:
                stage_conflict(pid, project, epoch)

        def stage_conflict(pid: str, project: dict[str, Any], epoch: FacilityRecord) -> None:
            """The row is held as this Epoch record's twin; its stage, which is not applied, may
            disagree with the record's status (Google Fort Wayne: Operating in AI GridWatch,
            under construction in Epoch's count of AI buildings)."""
            stage = clean_text(project.get("stage"))
            try:
                cw = from_aigridwatch_stage(
                    stage,
                    has_filing=has_filing(project),
                    no_application=no_application(project) is not None,
                )
            except UnknownStatus:
                return
            if cw.status == epoch.status:
                return
            stats["epoch_stage_conflicts"] += 1
            flag(
                "conflict",
                pid,
                f"AI GridWatch's stage {stage!r} ({cw.status}) disagrees with the status of the "
                f"Epoch AI record ({epoch.status}); the row is held as that record's twin, so "
                "its stage is not applied: a reviewer checks which is right",
                record_id=epoch.id,
                stage=stage,
                as_of=clean_text(project.get("as_of")) or None,
                epoch_status=epoch.status,
                support=stage_support(project, cw.status, ctx.today),
            )

        def release_epoch(pid: str, release: RowRelease, twin: Twin) -> None:
            ids = [twin.record_id] if twin.record_id else list(twin.candidates)
            released(
                pid,
                release,
                "epoch",
                "possible_duplicate",
                epoch_why(twin),
                matched_by=twin.how,
                epoch_records=ids,
            )

        for project in projects:
            if not isinstance(project, dict) or any(f not in project for f in REQUIRED_FIELDS):
                flag("invalid", None, "a project row lacks required fields")
                continue
            pid = clean_text(project["id"])
            if not pid:
                flag("invalid", None, "a project row has an empty id")
                continue
            if project.get("verified") is not True:
                stats["unverified"] += 1
                flag(
                    "unverified_upstream",
                    pid,
                    "AI GridWatch marks this row unverified: a lead to check, not a fact to cite",
                )
                continue
            state = clean_text(project["state"]).upper()
            if state_by_abbr(state) is None:
                flag("invalid", pid, f"unknown state {state!r}")
                continue
            if state not in IN_SCOPE:
                flag("out_of_scope", pid, f"{state} is outside the 50 states and DC")
                continue
            release = releases.get(pid)
            free = set(release.release) if release else set()
            direct = epoch_sites.match(project, state)
            if direct is not None and direct[0] != "epoch_source":
                if release is None or "epoch" not in free:
                    held(pid, project, epoch_sites.copy_of_another(project, Twin(*direct)))
                    continue
                release_epoch(pid, release, Twin(*direct))
            other = foreign.get(pid)
            if other is not None:
                why = (
                    f"the row's id starts with the name of AI GridWatch row {other!r}, not with "
                    "its own: AI GridWatch has given it another project's id"
                )
                if release is None or "id" not in free:
                    stats["held_for_review"] += 1
                    flag(
                        "conflict",
                        pid,
                        f"{why}. Records are matched on the id, so the row is held for review, "
                        "not imported, until the id is its own",
                        id_of=other,
                    )
                    continue
                released(pid, release, "id", "conflict", why, id_of=other)
            stage = clean_text(project["stage"])
            unproposed = no_application(project)
            try:
                cw = from_aigridwatch_stage(
                    stage, has_filing=has_filing(project), no_application=unproposed is not None
                )
            except UnknownStatus:
                flag("unknown_status", pid, f"unknown AI GridWatch stage {stage!r}", stage=stage)
                continue
            if unproposed is not None and is_aigridwatch_application_stage(stage):
                flag(
                    "conflict",
                    pid,
                    f"AI GridWatch's stage {stage!r} implies an application, but the row (its "
                    f"note or an entry about its project) says {unproposed!r} and no filing is "
                    "known: the record is announced, not "
                    "proposed (07 §2.3); a reviewer checks whether an application was filed",
                    stage=stage,
                    note_says=unproposed,
                )
            mark, row_stats = len(review), Counter[str]()
            built = self._record(
                project,
                pid=pid,
                state=state,
                stage=stage,
                status=cw.status,
                status_reason=cw.status_reason,
                rumor=cw.evidence_level == "rumor",
                ctx=ctx,
                generated_day=generated_day,
                retrieved_at=retrieved_at,
                gazetteer=gazetteer,
                counties=counties,
                places=places,
                areas=areas,
                flag=flag,
                released=released,
                release=release,
                stats=row_stats,
                existing=ctx.records.get(stored_agw.get(pid, "")),
            )
            if built is None:
                stats.update(row_stats)
                continue
            record, holds = built
            if direct is not None and direct[0] == "epoch_source":
                twin = Twin("epoch_source", None, epoch_sites.candidates(project, record))
                if release is None or "epoch" not in free:
                    del review[mark:]  # the row's own items: it is not imported
                    held(pid, project, twin)
                    continue
                release_epoch(pid, release, twin)
            elif direct is None:  # a released name, id or source match skips the location rules
                found = epoch_sites.twin(project, record)
                if found is not None and (release is None or "epoch" not in free):
                    del review[mark:]  # the row's own items: it is not imported
                    held(pid, project, epoch_sites.copy_of_another(project, found))
                    continue
                if found is not None and release is not None:
                    release_epoch(pid, release, found)
            if holds:
                stats["held_for_review"] += 1  # the row's items stay: they say why
                continue
            stats.update(row_stats)
            candidates.append(Candidate((pid,), record))

        planned = sum(1 for c in candidates for e in c.record.status_history if e.planned)
        metrics: dict[str, float | int] = {
            "projects": len(projects),
            "declared_count": int(doc["count"]) if isinstance(doc.get("count"), int) else -1,
            "unverified": stats["unverified"],
            "candidates": len(candidates),
            "planned_events": planned,
            "stage_events": stats["stage_events"],
            "stage_events_undated": stats["stage_events_undated"],
            "first_reported_events": stats["first_reported_events"],
            "hearings_unconfirmed": stats["hearings_unconfirmed"],
            "located_source_coords": stats["source_coords"],
            "located_gazetteer": stats["gazetteer"],
            "located_county_centroid": stats["county_centroid"],
            "persons_dropped": stats["persons_dropped"],
            "out_of_scope": stats["out_of_scope"],
            "epoch_duplicates": stats["epoch_duplicates"],
            "epoch_ambiguous": stats["epoch_ambiguous"],
            "epoch_stage_conflicts": stats["epoch_stage_conflicts"],
            "epoch_records_seen": len(epoch_sites.by_name),
            "held_for_review": stats["held_for_review"],
            "overrides_used": sum(1 for pid in releases if pid in row_ids),
            "overrides_unused": sum(1 for pid in releases if pid not in row_ids),
            "released": stats["released"],
            "upstream_events_unused": sum(
                len(p["events"])
                for p in projects
                if isinstance(p, dict) and isinstance(p.get("events"), list)
            ),
        }
        return ImportResult(self.name, [snapshot], candidates, review, metrics)

    @staticmethod
    def _place_by_text(
        loc: Locality, state: str, named: list[County], gazetteer: Gazetteer, counties: CountyIndex
    ) -> tuple[GeocodeResult, bool] | None:
        """A point from the locality text, for a row whose coordinates lie outside every county
        it names: the Gazetteer place when it lies in one of them, else the one named county's
        point on surface. With the bool: the place is only one the site is near."""
        from atlas.geocode import GeocodeRequest, geocode

        locality = next((t for t in (loc.place, loc.hint) if t and gazetteer.place(state, t)), None)
        nearby = locality is not None and locality == loc.hint and loc.hint_is_nearby
        for county in named if locality else []:
            req = GeocodeRequest(state_abbr=state, locality=locality, county_name=county.name)
            result = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
            if result is not None and result.method == "gazetteer":
                return result, nearby
        if len(named) == 1:
            req = GeocodeRequest(state_abbr=state, county_name=named[0].name)
            result = geocode(req, census=None, gazetteer=gazetteer, counties=counties)
            return (result, False) if result is not None else None
        return None

    def _record(
        self,
        project: dict[str, Any],
        *,
        pid: str,
        state: str,
        stage: str,
        status: Status,
        status_reason: StatusReason | None,
        rumor: bool,
        ctx: ImportContext,
        generated_day: date,
        retrieved_at: str,
        gazetteer: Gazetteer,
        counties: CountyIndex,
        places: PlaceIndex,
        areas: Mapping[str, float],
        flag: Callable[..., None],
        released: Callable[..., None],
        release: RowRelease | None,
        stats: Counter[str],
        existing: FacilityRecord | None = None,
    ) -> tuple[FacilityRecord, tuple[str, ...]] | None:
        """The row's record, and the checks that hold it for review ("stage": the event log
        reports a later milestone). None when no record can be made; the review items say why.
        existing is the stored record for the row, if any."""
        from atlas.geocode import GeocodeRequest, geocode

        free = set(release.release) if release else set()
        holds: list[str] = []

        # Location ---------------------------------------------------------------------
        locality_text = clean_text(project["locality"])
        loc = parse_locality(locality_text, state)
        lat, lon = parse_number(project.get("lat")), parse_number(project.get("lon"))
        place_city = place_municipality = None
        if loc.place:
            if _MUNICIPALITY_RE.search(loc.place):
                place_municipality = loc.place
            else:
                place_city = loc.place
        location: dict[str, Any] | None = None
        result: GeocodeResult | None = None
        nearby = False
        town = False  # the text's place is a county subdivision (a town), not a Census place
        if lat is not None and lon is not None:
            if not (18 <= lat <= 72 and -180 <= lon <= -64) or not counties.in_state(
                state, lat, lon
            ):
                flag(
                    "county_mismatch",
                    pid,
                    f"({lat}, {lon}) is not inside {state}",
                    locality=locality_text,
                )
                return None
            county = resolve_county(loc, state, counties, (lat, lon))
            named = named_counties(loc, state, counties)
            if county is None and named:
                # The locality text names the county, and the point is outside it: the text is
                # taken over the coordinates (EdgeCore's Louisa County campus had a point in
                # Goochland County), so no record shows a point known to be in the wrong county.
                names = " / ".join(f"{c.name} County" for c in named)
                placed = self._place_by_text(loc, state, named, gazetteer, counties)
                if placed is None:
                    flag(
                        "geocode_failed",
                        pid,
                        f"({lat}, {lon}) is not inside {names}, and neither a Gazetteer place in "
                        "it nor a single named county gives a point",
                        locality=locality_text,
                    )
                    return None
                result, nearby = placed
                placed_at = (
                    f"the Gazetteer place {result.city}"
                    if result.method == "gazetteer"
                    else f"the point of {names}"
                )
                flag(
                    "county_mismatch",
                    pid,
                    f"({lat}, {lon}) is not inside {names}: the coordinates are not used, and the "
                    f"record is placed at {placed_at} instead",
                    locality=locality_text,
                    lat=lat,
                    lon=lon,
                )
            else:
                if county is None and loc.county_texts:
                    flag(
                        "county_mismatch",
                        pid,
                        f"{' / '.join(loc.county_texts)} is not a county the county file knows; "
                        "the record keeps the point and names no county",
                        locality=locality_text,
                    )
                at = county or counties.lookup(lat, lon)
                checked = check_place(
                    loc, state, at.fips if at else None, lat, lon, gazetteer, places, areas
                )
                for name, why in checked.dropped:
                    flag(
                        "county_mismatch",
                        pid,
                        f"({lat}, {lon}) is not shown to lie in {name}, which the locality names "
                        f"({why}): the record keeps the point and does not name it",
                        locality=locality_text,
                        place=name,
                        lat=lat,
                        lon=lon,
                    )
                location = {
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "precision": "locality",
                    "geocode_method": "source_coords",
                    "city": checked.city,
                    "municipality": checked.municipality,
                    "county_name": county.name if county else None,
                    "county_fips": county.fips if county else None,
                    "state_abbr": state,
                }
                stats["source_coords"] += 1
        else:
            county = resolve_county(loc, state, counties, None)
            # A locality that names no county ("Fredericksburg", a market name) is placed in the
            # county the row's note and event log state, when they all state one (n1: Vantage
            # VA4 is "on 82 acres in Stafford County near Fredericksburg").
            stated = None
            if county is None and not loc.county_texts:
                stated = county = stated_county(project, state, counties)
            # A Census place, or a county subdivision (Bloomfield, CT is a town; the Gazetteer's
            # only place there is Blue Hills CDP), in the named county when there is one.
            locality = next(
                (
                    t
                    for t in (loc.place, loc.hint)
                    if t and gazetteer.has_locality(state, t, county.fips if county else None)
                ),
                None,
            )
            # A nearby place ("Storey County (near Reno)") may give the point, never the city.
            nearby = locality is not None and locality == loc.hint and loc.hint_is_nearby
            result = geocode(
                GeocodeRequest(
                    state_abbr=state,
                    locality=locality,
                    county_name=county.name if county else None,
                ),
                census=None,
                gazetteer=gazetteer,
                counties=counties,
            )
            if result is None:
                flag(
                    "geocode_failed",
                    pid,
                    "no coordinates, and neither a Gazetteer place nor a county matches "
                    "the locality",
                    locality=locality_text,
                )
                return None
            town = result.municipality is not None and result.city is None and locality == loc.place
            if loc.place and locality != loc.place:
                # The text's place is no Census place or subdivision ("Globeville-Elyria-Swansea
                # (Denver)", a neighborhood): the record names the place it is placed at.
                place_city = place_municipality = None
            if result.method == "county_centroid":
                # Placed at the county's point: the text's place is not in that county (or not
                # known), so the record does not name it.
                place_city = place_municipality = None
                if stated is not None and locality is not None:
                    flag(
                        "county_mismatch",
                        pid,
                        f"the locality names no county, and the row's note and event log place the "
                        f"site in {stated.name} County, which does not contain {locality}: the "
                        f"record is placed at the county's point and does not name {locality}",
                        locality=locality_text,
                        stated_county=stated.fips,
                        place=locality,
                    )
            elif (
                result.county_fips is None
                and result.city
                and result.lat is not None
                and result.lon is not None
            ):
                # A Virginia independent city is its own county-equivalent (Fredericksburg city,
                # 51630): its county is then known.
                at = counties.lookup(result.lat, result.lon)
                full = gazetteer.county_full_name(at.fips) if at else None
                if at and full and normalize_name(full) == normalize_name(f"{result.city} city"):
                    result = dataclasses.replace(result, county_fips=at.fips, county_name=at.name)
        if location is None and result is not None:
            if town:
                place_city = None  # Bloomfield, CT: the town is the municipality, not a city
            location = {
                "lat": result.lat,
                "lon": result.lon,
                "precision": result.precision,
                "geocode_method": result.method,
                "city": place_city or (None if nearby else result.city),
                "municipality": place_municipality or (None if nearby else result.municipality),
                "county_name": result.county_name,
                "county_fips": result.county_fips,
                "state_abbr": state,
            }
            stats[result.method or "none"] += 1
        if location is None:
            return None
        # A place the row's own words put the site outside of is never named, whatever the point
        # says (n0, n5, n32): the point is then that place's, not the site's, and tells only the
        # county.
        for key in ("city", "municipality"):
            named_place = location.get(key)
            outside = outside_phrase(project, named_place) if named_place else None
            if outside is None:
                continue
            out_phrase, out_where = outside
            location[key] = None
            if location.get("county_fips"):
                location["precision"] = "county"
            flag(
                "county_mismatch",
                pid,
                f"{out_where} puts the site outside {named_place} ({out_phrase!r}): the record does "
                f"not name {named_place}"
                + (
                    ", and keeps the point only at county precision"
                    if location.get("county_fips")
                    else ""
                ),
                locality=locality_text,
                place=named_place,
                phrase=out_phrase,
            )

        # Parties and aliases ----------------------------------------------------------
        ref = {"source_ids": ["s1"]}

        def orgs(role: str, names: list[str]) -> list[str]:
            out: list[str] = []
            for text in names:
                party = party_name(text)
                if party.person_dropped:
                    stats["persons_dropped"] += 1
                if party.capacity:
                    flag(
                        "unit_parse",
                        pid,
                        f"{role} holds a capacity, not an organization; it is not imported",
                        **{role: text},
                    )
                if party.name and party.name.casefold() not in {n.casefold() for n in out}:
                    out.append(party.name)
            return out

        def field_names(role: str, separators: str) -> list[dict[str, Any]]:
            names = split_names(clean_text(project.get(role)), separators)
            return [{"name": n, **ref} for n in orgs(role, names)]

        filings = orgs("filing_llc", filing_names(clean_text(project.get("filing_llc"))))
        # AI GridWatch's operator field is "Operator/developer": the role it guarantees is the
        # developer's (a real estate firm or a builder is no operator). An electric utility in it
        # is the one that supplies or approves the site, not its developer: left out, with an item.
        developers = []
        for party in field_names("operator", r"\s+/\s+"):
            replaced = party_replaced(project, party["name"])
            if replaced is not None:
                flag(
                    "conflict",
                    pid,
                    f"AI GridWatch's operator/developer field names {party['name']!r}, but the "
                    f"row's own text says it is being replaced as the developer ({replaced!r}): it "
                    "is not imported, and a reviewer names the developer",
                    operator=party["name"],
                )
                continue
            if _UTILITY_RE.search(party["name"]):
                flag(
                    "unit_parse",
                    pid,
                    f"AI GridWatch's operator/developer field names {party['name']!r}, an "
                    "electric utility: a utility that supplies or approves a site is not its "
                    "developer, so it is not imported",
                    operator=party["name"],
                )
                continue
            developers.append(party)
        parties = {
            "operator": [],
            "developer": developers,
            "owner": field_names("owner", r"\s+/\s+"),
            "tenant": field_names("tenant", r"\s+/\s+|\s*,\s+"),
            "filing_entities": [{"name": n, **ref} for n in filings],
        }
        aliases = [{"name": n, "kind": "filing_llc", **ref} for n in filings]

        # Scope ------------------------------------------------------------------------
        scope = "in_scope"
        phrase = not_a_data_center(project)
        if phrase is not None:
            why = (
                f"the row says {phrase!r}: a power supply deal or a generation facility, not a "
                "data center site (07 §2.2)"
            )
            if release is not None and "scope" in free:
                released(pid, release, "scope", "out_of_scope", why)
            else:
                scope = "out_of_scope"
                stats["out_of_scope"] += 1
                flag(
                    "out_of_scope",
                    pid,
                    f"{why}; the record is kept out of scope, so it is never published",
                    phrase=phrase,
                )

        # Capacity and site ------------------------------------------------------------
        imported = {"confidence": CONFIDENCE, "method": "imported", "source_ids": ["s1"]}
        field_meta: dict[str, Any] = {
            "/location": {
                "confidence": CONFIDENCE if location["geocode_method"] == "source_coords" else 0.60,
                "method": "imported"
                if location["geocode_method"] == "source_coords"
                else "derived",
                "source_ids": ["s1"],
            }
        }
        capacity: dict[str, Any] = {}
        site: dict[str, Any] = {}
        size_mw = parse_number(project.get("size_mw"))
        if size_mw is not None and 0 < size_mw <= MW_MAX:
            # The figure always stays as stated; a basis only when the row's note gives one.
            capacity["mw_as_stated"] = f"AI GridWatch size_mw: {size_mw:g}"
            basis = size_basis(size_mw, clean_text(project.get("note")))
            others = (
                other_basis_figures(project, size_mw, basis)
                if basis in ("it", "utility_request")
                else []
            )
            if others:
                what = "critical IT load" if basis == "it" else "a supply or interconnection"
                flag(
                    "unit_parse",
                    pid,
                    f"size_mw {size_mw:g} is {what} by the row's note, but its event log gives the "
                    f"campus other figures on that basis ({'; '.join(others[:3])}): only "
                    "mw_as_stated keeps it, and a reviewer sets the figure",
                    size_mw=f"{size_mw:g}",
                    basis=basis,
                    others=others[:5],
                )
                basis = None
            if basis == "it":
                capacity["it_mw"] = size_mw
                field_meta["/capacity/it_mw"] = imported
            elif basis == "utility_request":
                capacity["utility_request_mw"] = size_mw
                field_meta["/capacity/utility_request_mw"] = imported
            elif basis is not None:
                what = {
                    "power_source": "a power source's capacity (a plant, a reactor or its output)",
                    "phase": "one phase's",
                }[basis]
                flag(
                    "unit_parse",
                    pid,
                    f"size_mw {size_mw:g} is {what}, by the row's note, not the data center's: "
                    "only mw_as_stated keeps it",
                    size_mw=f"{size_mw:g}",
                    basis=basis,
                )
        elif size_mw is not None or clean_text(project.get("size_mw")):
            flag(
                "unit_parse",
                pid,
                "size_mw is not a number in (0, 10000]",
                size_mw=str(project.get("size_mw")),
            )
        acres = parse_number(project.get("acres"))
        campus = campus_acres(project)
        if (
            acres is not None
            and 0 < acres <= ACRES_MAX
            and campus is not None
            and not _same_acres(campus[0], acres)
        ):
            # The row's acres are the whole property's when its own log gives the named campus
            # another acreage (n36: "the 304-acre BCG Cedar Creek Campus portion of the
            # 2,842-acre development").
            site["acreage"] = campus[0]
            field_meta["/site/acreage"] = imported
            flag(
                "unit_parse",
                pid,
                f"acres {acres:g} is the whole property's: the row's own text gives the named "
                f"campus {campus[0]:g} acres ({campus[1]!r}), so the record publishes that",
                acres=f"{acres:g}",
                campus_acres=f"{campus[0]:g}",
            )
        elif acres is not None and 0 < acres <= ACRES_MAX:
            site["acreage"] = acres
            field_meta["/site/acreage"] = imported
        elif acres is not None or clean_text(project.get("acres")):
            flag(
                "unit_parse",
                pid,
                "acres is not a number in (0, 100000]",
                acres=str(project.get("acres")),
            )

        # Status history ---------------------------------------------------------------
        late = late_announcement(project)
        if late is not None:
            flag(
                "conflict",
                pid,
                f"AI GridWatch dates the announcement ({late[0].isoformat()}) after {late[1]} "
                f"({clean_text(project.get(late[1]))}); the announced milestone is left out, so "
                "the status history does not move backward",
                announced=late[0].isoformat(),
                **{late[1]: clean_text(project.get(late[1]))},
            )
        explained = announcement(project)
        if explained.doubt is not None:
            flag(
                "conflict",
                pid,
                f"AI GridWatch's announced date ({clean_text(project.get('announced'))}) is not "
                f"imported as an announcement: {explained.doubt}",
                announced=clean_text(project.get("announced")),
                filing=explained.filing.fuzzy()["value"] if explained.filing else None,
            )
        filing = rezoning_filing(project)
        if filing.doubt is not None:
            flag(
                "conflict",
                pid,
                f"AI GridWatch's rezoning_filed date ({clean_text(project.get('rezoning_filed'))}) "
                f"is not imported as the application's filing: {filing.doubt}"
                + (
                    ""
                    if filing.day is not None
                    else "; no application_filed date is imported, and a reviewer dates the "
                    "application if there is one"
                ),
                rezoning_filed=clean_text(project.get("rezoning_filed")),
                application_filed=filing.day.fuzzy()["value"] if filing.day else None,
            )
        decision = decision_day(project)
        if decision.moved_by is not None and decision.day is not None:
            flag(
                "conflict",
                pid,
                f"AI GridWatch's decided_date ({clean_text(project.get('decided_date'))}) is the day "
                f"after the meeting the row says the vote ended ({decision.moved_by!r}): the "
                f"decision is dated {decision.day.fuzzy()['value']}, the meeting's day",
                decided_date=clean_text(project.get("decided_date")),
                dated=decision.day.fuzzy()["value"],
            )
        events = milestone_events(project, ctx.today)
        approval = unnamed_approval(project)
        if approval is not None:
            day_entries = [
                clean_text(e.get("summary"))
                for e in row_events(project)
                if (d := entry_date(e)) is not None and _same_period(d, approval)
            ]
            flag(
                "conflict",
                pid,
                f"AI GridWatch's decision of {clean_text(project.get('decided_date'))} has outcome "
                "approved, but neither the row's note nor its event log for that day names a "
                "land-use approval or a permit (a rezoning, a conditional or special use, a site "
                "or development plan, a PUD, a variance or a permit): no approved event is "
                "imported, and the stage stays an observation that dates nothing",
                decided_date=clean_text(project.get("decided_date")),
                stage=stage,
                entry=day_entries[0][:300] if day_entries else None,
            )
        placeholders = [e for e in row_events(project) if placeholder_source(e)]
        if placeholders:
            flag(
                "unverified_upstream",
                pid,
                f"{len(placeholders)} of the row's {len(row_events(project))} event-log entries "
                f"cite a placeholder link, not a source "
                f"({clean_text(placeholders[0].get('source'))}): they date nothing in the record "
                "(no first report, filing, hearing or approval is taken from them)",
                entries=[clean_text(e.get("date")) or None for e in placeholders],
            )
        hearing = unconfirmed_hearing(project, ctx.today)
        if hearing is not None:
            stats["hearings_unconfirmed"] += 1
            flag(
                "unknown_status",
                pid,
                f"AI GridWatch's hearing_date ({hearing.fuzzy()['value']}) has passed, but no "
                "decision on that day and no entry of the row's event log for that day says the "
                "hearing was held (hearings get moved): no hearing event is imported",
                hearing_date=clean_text(project.get("hearing_date")),
            )
        first = first_report(project, status, events, ctx.today)
        if first.event is None and release is not None and release.first_reported is not None:
            first = FirstReport(
                {
                    "seq": 0,
                    "status": "announced",
                    "event": "first_reported",
                    "as_of": AgwDate(release.first_reported, "day").fuzzy(),
                    "planned": False,
                    "source_ids": ["s1"],
                    "note": (
                        "First report dated by a reviewer "
                        f"({release.reviewed_at.isoformat()}, config/overrides/aigridwatch.json)"
                    ),
                },
                earliest=AgwDate(release.first_reported, "day"),
            )
        if first.event is not None:
            events.insert(0, first.event)
            for i, e in enumerate(events):
                e["seq"] = i + 1
            stats["first_reported_events"] += 1
        dated = [
            period_start(FuzzyDate.model_validate(e["as_of"])) for e in events if not e["planned"]
        ]
        derived = min(dated) if dated else None
        doubt_data = {
            "event_date": clean_text(first.entry.get("date")) or None if first.entry else None,
            "event_kind": clean_text(first.entry.get("kind")) or None if first.entry else None,
            "event_source": clean_text(first.entry.get("source")) or None if first.entry else None,
        }
        if first.earliest is not None and derived is not None and first.earliest.end < derived:
            # dates.first_reported is the earliest dated event (rollup.derive_dates): imported,
            # the record would say the project was first reported later than its own row shows.
            why = (
                f"the row shows the project public by {first.earliest.fuzzy()['value']}, before "
                f"its earliest dated milestone ({derived.isoformat()}), and "
                + (
                    first.doubt
                    if first.doubt
                    else "its event log gives no earlier report that can be dated"
                )
                + f": imported, its first_reported would be {derived.isoformat()}, later than "
                "the row shows"
            )
            if release is not None and "first_report" in free:
                released(pid, release, "first_report", "conflict", why, **doubt_data)
            else:
                holds.append("first_report")
                flag(
                    "conflict",
                    pid,
                    f"{why}. The row is held for review, not imported: a reviewer dates the first "
                    "report (first_reported in config/overrides/aigridwatch.json)",
                    earliest=first.earliest.fuzzy()["value"],
                    derived=derived.isoformat(),
                    **doubt_data,
                )
        elif first.event is None and (terminal := _terminal_first(events)) is not None:
            # A denial, a withdrawal or a moratorium comes after the project was public: it never
            # dates the first report (n41: Project Riverjump's only milestone is the mayor's
            # withdrawal of 2026-08-27, while its rumors circulated from January).
            why = (
                f"the row's earliest dated milestone is its {terminal['event']} event of "
                f"{terminal['as_of']['value']}, and "
                + (first.doubt if first.doubt else "its event log gives no earlier report")
                + f": imported, its first_reported would be that {terminal['event']} event's "
                "day, but a project is public before it is denied, withdrawn or paused"
            )
            if release is not None and "first_report" in free:
                released(pid, release, "first_report", "conflict", why, **doubt_data)
            else:
                holds.append("first_report")
                flag(
                    "conflict",
                    pid,
                    f"{why}. The row is held for review, not imported: a reviewer dates the first "
                    "report (first_reported in config/overrides/aigridwatch.json)",
                    earliest=terminal["as_of"]["value"],
                    derived=derived.isoformat() if derived else None,
                    **doubt_data,
                )
        elif first.doubt is not None:
            entry_day = entry_date(first.entry) if first.entry else None
            if derived is None:
                tail = "No first_reported event is imported"
            elif entry_day is not None and entry_day.start < derived:
                tail = (
                    f"The record's first_reported is its earliest milestone ({derived.isoformat()}), "
                    "and that entry is earlier: a reviewer checks whether it reports this project"
                )
            else:
                tail = ""
            if tail:
                flag(
                    "unknown_status",
                    pid,
                    f"AI GridWatch's event log does not tell when the project was first "
                    f"reported: {first.doubt}. {tail}",
                    **doubt_data,
                )
        # The earliest date the row shows the project public: a dated event, an entry naming it,
        # or the first entry that mentions a data center there.
        known = [
            period_start(FuzzyDate.model_validate(e["as_of"])) for e in events if not e["planned"]
        ]
        if first.earliest is not None:
            known.append(first.earliest.start)
        if first.entry is not None and (doubt_day := entry_date(first.entry)) is not None:
            known.append(doubt_day.start)
        log = log_milestone(project, status, ctx.today)
        obstacle = log is None
        if log is None:
            log = log_obstacle(project, status, ctx.today, min(known) if known else None)
        if log is not None:
            if not obstacle:
                behind = f"is behind its own event log, which reports {log.what}"
            elif status == "proposed":
                behind = (
                    f"implies an application under review, but its own event log reports {log.what}"
                )
            else:
                behind = f"is contradicted by its own event log, which reports {log.what}"
            why = f"AI GridWatch's stage {stage!r} ({status}) {behind} {log.where()}{log.detail}"
            evidence = {
                "stage": stage,
                "reported_status": log.status,
                "event_date": log.day.fuzzy()["value"] if log.day else None,
                "event_kind": log.kind or None,
                "event_source": log.source,
            }
            if release is not None and "stage" in free:
                released(pid, release, "stage", "conflict", why, **evidence)
            else:
                holds.append("stage")
                flag(
                    "conflict",
                    pid,
                    f"{why}: the row is held for review, not imported, rather than published "
                    "with the stage's status",
                    **evidence,
                )
        if status == "cancelled" and status_reason is None:
            status_reason = withdrawal_reason(project)
        latest = _latest_actual([StatusEvent.model_validate(e) for e in events])
        if latest is None or latest.status != status:
            as_of_text = clean_text(project.get("as_of"))
            observed = milestone_date(project, "as_of")
            note = f"AI GridWatch stage '{stage}' as of {as_of_text}"
            if observed is None and release is not None and release.as_of is not None:
                observed = AgwDate(release.as_of, "day")
                note = (
                    f"AI GridWatch stage '{stage}', confirmed by a reviewer on "
                    f"{release.as_of.isoformat()} (the row has no as_of)"
                )
            if observed is None:
                # Owner decision of 2026-10-09: the stage the row reports is an observation in
                # the file of its generated date. An `other` event sets the status and dates
                # nothing (rollup.derive_dates skips it), as for an OSM tag. The first file that
                # showed the stage keeps its date, so a weekly run does not move it.
                note = UNDATED_STAGE_NOTE.format(stage=stage)
                observed = AgwDate(generated_day, "day")
                seen = [
                    e.as_of
                    for e in (existing.status_history if existing is not None else [])
                    if e.event == "other"
                    and not e.planned
                    and e.status == status
                    and e.note == note
                    and period_start(e.as_of) < generated_day
                ]
                if seen:
                    observed = AgwDate(period_start(min(seen, key=period_start)), "day")
                stats["stage_events_undated"] += 1
            if latest is not None and observed.start < period_start(latest.as_of):
                observed = AgwDate.of(latest.as_of)
            events.append(
                {
                    "seq": len(events) + 1,
                    "status": status,
                    "event": "other",
                    "as_of": observed.fuzzy(),
                    "planned": False,
                    "source_ids": ["s1"],
                    "note": note,
                }
            )
            stats["stage_events"] += 1

        # Sources ----------------------------------------------------------------------
        sources: list[dict[str, Any]] = [
            {
                "id": "s1",
                "url": DATASET_URL,
                "publisher": "AI GridWatch",
                "title": "AI GridWatch data center project tracker",
                "source_type": "open_dataset",
                "license": LICENSE,
                "retrieved_at": retrieved_at,
                "supports": list(S1_SUPPORTS),
            }
        ]
        row_url = clean_text(project.get("source"))
        if row_url:
            try:
                HttpUrl(row_url)
            except ValidationError:
                row_url = ""
        if row_url:
            sources.append(
                {
                    "id": "s2",
                    "url": row_url,
                    "publisher": host_of(row_url),
                    "source_type": classify_source(row_url),
                    "retrieved_at": retrieved_at,
                    "supports": [],
                }
            )

        name = clean_text(project["name"]) or pid
        where = location.get("city") or location.get("municipality")
        if not where and location.get("county_fips"):
            where = gazetteer.county_full_name(location["county_fips"])
        canonical = f"{name} ({where}, {state})" if where else f"{name} ({state})"
        ts = ctx.now.isoformat()
        doc: dict[str, Any] = {
            "id": PLACEHOLDER_ID,
            "record_type": "project",
            "scope": scope,
            "canonical_name": canonical,
            "aliases": aliases,
            "parties": parties,
            "purpose": "unknown",
            "status": status,
            "status_reason": status_reason,
            "evidence_level": "rumor" if rumor else "reported",
            "status_history": events,
            "location": location,
            "capacity": capacity,
            "site": site,
            "external_ids": {"aigridwatch_id": [pid]},
            "sources": sources,
            "field_meta": field_meta,
            "review": {"state": "machine"},
            "created_at": ts,
            "updated_at": ts,
            "last_verified_at": ts,
        }
        try:
            return apply_rollup(FacilityRecord.model_validate(doc)), tuple(holds)
        except (ValidationError, RollupError) as e:
            flag("invalid", pid, "the row does not map to a valid record", error=str(e))
            return None


IMPORTER = AIGridWatchImporter()
