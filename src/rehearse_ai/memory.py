"""Small, session-scoped facts supplied by the candidate."""

from dataclasses import dataclass, field
from typing import Literal

FactField = Literal[
    "candidate_name",
    "project_name",
    "project_purpose",
    "contribution",
    "technical_challenges",
    "testing_evidence",
    "technical_terms",
    "technologies",
]
FACT_FIELDS: tuple[FactField, ...] = (
    "candidate_name",
    "project_name",
    "project_purpose",
    "contribution",
    "technical_challenges",
    "testing_evidence",
    "technical_terms",
    "technologies",
)


@dataclass(frozen=True)
class Fact:
    value: str
    source_turn: int | None
    supplied_wording: str
    confirmed: bool = False


@dataclass(frozen=True)
class Correction:
    field: FactField
    previous: Fact | None
    replacement: Fact
    source_turn: int | None
    superseded_wording: str


@dataclass
class SessionMemory:
    facts: dict[FactField, Fact] = field(default_factory=dict)
    corrections: list[Correction] = field(default_factory=list)

    def get(self, field_name: FactField) -> Fact | None:
        return self.facts.get(field_name)

    def supply(
        self,
        field_name: FactField,
        value: str,
        *,
        source_turn: int | None,
        supplied_wording: str,
        confirmed: bool = False,
        correction: bool = False,
        superseded_wording: str | None = None,
    ) -> Fact:
        value = value.strip()
        if not value or len(value) > 240:
            raise ValueError("Memory facts must be short, nonempty candidate wording")
        previous = self.facts.get(field_name)
        if previous and not correction:
            if previous.value.casefold() == value.casefold():
                if confirmed and not previous.confirmed:
                    fact = Fact(previous.value, source_turn, supplied_wording, True)
                    self.facts[field_name] = fact
                    return fact
                return previous
            if previous.confirmed:
                return previous
        fact = Fact(value, source_turn, supplied_wording, confirmed)
        self.facts[field_name] = fact
        if correction:
            self.corrections.append(
                Correction(
                    field_name,
                    previous,
                    fact,
                    source_turn,
                    superseded_wording or (previous.value if previous else ""),
                )
            )
        return fact

    def for_model(self) -> dict[str, dict[str, str | bool]]:
        return {
            field_name: {"value": fact.value, "confirmed": fact.confirmed}
            for field_name, fact in self.facts.items()
        }

    def corrections_for_model(self) -> list[dict[str, str]]:
        return [
            {
                "field": correction.field,
                "superseded": correction.superseded_wording,
                "current": correction.replacement.value,
            }
            for correction in self.corrections[-4:]
        ]
