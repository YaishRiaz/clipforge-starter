"""Rubric files, and the arithmetic that turns category votes into a score.

PRD section 6 step 4: scoring uses a rubric file per niche, and the weights are
re-weighted weekly from our own clips' view counts. Keeping the weights in a
file and the arithmetic here means the model judges *what is in* a moment while
the weights decide *what that is worth* — so re-weighting never needs a prompt
change, and two moments are always compared the same way.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("clipforge.moments.rubric")


class RubricError(RuntimeError):
    pass


@dataclass(frozen=True)
class Category:
    id: str
    weight: float
    description: str


@dataclass(frozen=True)
class Rubric:
    name: str
    version: int
    categories: tuple[Category, ...]
    penalties: tuple[Category, ...]

    @property
    def category_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.categories)

    @property
    def penalty_ids(self) -> tuple[str, ...]:
        return tuple(p.id for p in self.penalties)

    def weight_of(self, ident: str) -> float:
        for item in (*self.categories, *self.penalties):
            if item.id == ident:
                return item.weight
        return 0.0

    def describe_for_prompt(self) -> str:
        """The rubric as the model sees it: ids, not weights.

        The weights are deliberately withheld. If the model knew that
        `stakes_early` scores highest it would start finding it everywhere;
        its job is to say what is actually there.
        """
        lines = ["What to look for:"]
        lines += [f"- {c.id}: {c.description}" for c in self.categories]
        lines += ["", "What counts against a moment:"]
        lines += [f"- {p.id}: {p.description}" for p in self.penalties]
        return "\n".join(lines)

    def score(self, categories: list[str], penalties: list[str], *,
              base: float = 0.0) -> float:
        """Weighted score for one moment, as a 0-100 number.

        An unknown id contributes nothing rather than raising: the rubric can
        gain and lose categories between runs, and an old moment must still be
        comparable.
        """
        total = base
        for ident in dict.fromkeys(categories):        # ignore duplicates
            weight = self.weight_of(ident)
            if weight == 0.0 and ident not in self.category_ids:
                log.debug("unknown rubric category %r", ident)
            total += max(0.0, weight)
        for ident in dict.fromkeys(penalties):
            total += min(0.0, self.weight_of(ident))
        # The best possible moment hits every category and no penalty.
        ceiling = sum(max(0.0, c.weight) for c in self.categories) or 1.0
        return round(max(0.0, min(1.0, total / ceiling)) * 100, 1)


def _category(raw: dict, where: str) -> Category:
    try:
        return Category(str(raw["id"]), float(raw["weight"]), str(raw.get("description", "")))
    except (KeyError, TypeError, ValueError) as exc:
        raise RubricError(f"bad {where} entry {raw!r}: {exc}") from exc


def load(path: Path) -> Rubric:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RubricError(f"no rubric at {path}") from exc
    except json.JSONDecodeError as exc:
        raise RubricError(f"{path} is not valid JSON: {exc}") from exc
    categories = tuple(_category(c, "category") for c in raw.get("categories", []))
    if not categories:
        raise RubricError(f"{path} lists no categories")
    return Rubric(
        name=str(raw.get("name") or Path(path).stem),
        version=int(raw.get("version", 1)),
        categories=categories,
        penalties=tuple(_category(p, "penalty") for p in raw.get("penalties", [])),
    )


@lru_cache(maxsize=8)
def for_campaign(rubrics_dir: Path, category: str | None) -> Rubric:
    """The rubric for a campaign's niche, falling back to `general`.

    A niche without its own file (anything but `gta` so far) uses the general one.
    """
    if category:
        candidate = Path(rubrics_dir) / f"{category.strip().lower()}.json"
        if candidate.is_file():
            return load(candidate)
    return load(Path(rubrics_dir) / "general.json")
