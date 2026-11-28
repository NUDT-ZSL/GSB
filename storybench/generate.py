"""Generate the deterministic story corpus and three grading standards."""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from .text import detokenize, tokenize, word_count


GENERATOR_NAME = "storybench-deterministic-generator"
DEFAULT_SEED = 20261128


def _entry(pos: str, semantic_class: str, number: str | None = None) -> dict[str, str]:
    data = {"pos": pos, "class": semantic_class}
    if number:
        data["number"] = number
    return data


LEXICON: dict[str, dict[str, str]] = {}


def _add_words(pos: str, semantic_class: str, values: str, number: str | None = None) -> None:
    for word in values.split():
        LEXICON[word] = _entry(pos, semantic_class, number)


_add_words("DET", "determiner", "a an the")
_add_words("DET", "quantifier", "some any")
_add_words(
    "PRON",
    "pronoun",
    "i you he she it we they me him her us them this that these those",
)
_add_words("CONJ", "conjunction", "and or but because while when then so")
_add_words(
    "PREP",
    "preposition",
    "to of in on at for with from by near under over toward behind beside above "
    "across down into inside around after before",
)
_add_words(
    "AUX",
    "auxiliary",
    "was were is are be been am could would should can will did do does had has have",
)
_add_words("NEG", "negation", "not")
_add_words(
    "ADV",
    "adverb",
    "rapidly slowly quickly fast very then soon quietly loudly happily sadly "
    "carefully suddenly still",
)
_add_words(
    "ADJ",
    "size",
    "gigantic enormous huge big large tiny small little tall short long high low",
)
_add_words("ADJ", "color", "red blue green brown gray dark bright")
_add_words("ADJ", "age", "old new ancient young")
_add_words(
    "ADJ",
    "quality",
    "quiet calm silent loud soft warm cold happy sad kind tired hungry busy heavy "
    "light clean dirty open closed",
)
_add_words(
    "NOUN",
    "person",
    "maya leo nina omar ava ben shopkeeper worker farmer child vet veterinarian "
    "doctor teacher friend",
)
_add_words("PROPN", "person_name", "maya leo nina omar ava ben")
_add_words(
    "NOUN",
    "animal",
    "fox bird dog cat horse fish",
)
_add_words(
    "NOUN",
    "place",
    "bridge road shop tree wall house gate yard station city market hill sky "
    "school park river home garden",
)
_add_words(
    "NOUN",
    "object",
    "drum train blanket box bag kite food blanket book ball coat hat",
)
_add_words(
    "VERB_PAST",
    "action_past",
    "saw heard found ran followed gave smiled discovered carried placed walked "
    "commenced clapped observed assisted thanked boarded purchased ate moved chased "
    "returned laughed played noticed watched helped bought started began brought "
    "asked called opened closed cleaned cooked danced jumped kicked looked loved "
    "needed painted pulled pushed read sang sat slept spoke stood talked taught "
    "threw waited wanted washed wrote",
)
_add_words(
    "VERB_BASE",
    "action_base",
    "see hear find run follow give smile discover carry place walk commence clap "
    "observe assist thank board purchase eat move chase return laugh play notice "
    "watch help buy start begin bring ask call close clean cook dance jump "
    "kick look love need paint pull push read sing sit sleep speak stand talk teach "
    "throw wait want wash write fly",
)
_add_words("VERB_ING", "action_ing", "playing smiling running flying walking")


SYNONYM_TABLE: list[dict[str, Any]] = [
    {
        "source": "perceived",
        "options": [("saw", "saw"), ("noticed", "noticed")],
        "pos": "VERB_PAST",
        "levels": {"L1": "L1-SYN-001", "L2": "L2-SYN-001", "L3": "L3-SYN-001"},
    },
    {
        "source": "gigantic",
        "options": [("big", "big"), ("huge", "huge")],
        "pos": "ADJ",
        "levels": {"L1": "L1-SYN-002", "L2": "L2-SYN-002", "L3": "L3-SYN-002"},
    },
    {
        "source": "ancient",
        "options": [("old", "old")],
        "pos": "ADJ",
        "levels": {"L1": "L1-SYN-003", "L2": "L2-SYN-003", "L3": "L3-SYN-003"},
    },
    {
        "source": "rapidly",
        "options": [("fast", "fast"), ("quickly", "quickly")],
        "pos": "ADV",
        "levels": {"L1": "L1-SYN-004", "L2": "L2-SYN-004", "L3": "L3-SYN-004"},
    },
    {
        "source": "quiet",
        "options": [("calm", "calm"), ("silent", "silent")],
        "pos": "ADJ",
        "levels": {"L1": "L1-SYN-005", "L2": "L2-SYN-005", "L3": "L3-SYN-005"},
    },
    {
        "source": "exhausted",
        "options": [("tired", "tired")],
        "pos": "ADJ",
        "levels": {"L1": "L1-SYN-006", "L2": "L2-SYN-006", "L3": "L3-SYN-006"},
    },
    {
        "source": "discovered",
        "options": [("found", "found")],
        "pos": "VERB_PAST",
        "levels": {"L1": "L1-SYN-007", "L2": "L2-SYN-007", "L3": "L3-SYN-007"},
    },
    {
        "source": "beneath",
        "options": [("under", "under")],
        "pos": "PREP",
        "levels": {"L1": "L1-SYN-008", "L2": "L2-SYN-008", "L3": "L3-SYN-008"},
    },
    {
        "source": "veterinarian",
        "options": [("vet", "vet")],
        "pos": "NOUN",
        "levels": {"L1": "L1-SYN-009", "L2": "L2-SYN-009", "L3": "L3-SYN-009"},
    },
    {
        "source": "commenced",
        "options": [("started", "started"), ("began", "began")],
        "pos": "VERB_PAST",
        "levels": {"L1": "L1-SYN-010", "L2": "L2-SYN-010", "L3": "L3-SYN-010"},
    },
    {
        "source": "observed",
        "options": [("saw", "saw"), ("watched", "watched")],
        "pos": "VERB_PAST",
        "levels": {"L1": "L1-SYN-011", "L2": "L2-SYN-011", "L3": "L3-SYN-011"},
    },
    {
        "source": "rapid",
        "options": [("fast", "fast"), ("quick", "quick")],
        "pos": "ADJ",
        "levels": {"L1": "L1-SYN-012", "L2": "L2-SYN-012", "L3": "L3-SYN-012"},
    },
    {
        "source": "assisted",
        "options": [("helped", "helped")],
        "pos": "VERB_PAST",
        "levels": {"L1": "L1-SYN-013", "L2": "L2-SYN-013", "L3": "L3-SYN-013"},
    },
    {
        "source": "purchased",
        "options": [("bought", "bought")],
        "pos": "VERB_PAST",
        "levels": {"L1": "L1-SYN-014", "L2": "L2-SYN-014", "L3": "L3-SYN-014"},
    },
    {
        "source": "enormous",
        "options": [("big", "big"), ("huge", "huge")],
        "pos": "ADJ",
        "levels": {"L1": "L1-SYN-015", "L2": "L2-SYN-015", "L3": "L3-SYN-015"},
    },
    {
        "source": "gray",
        "options": [("dark", "dark")],
        "pos": "ADJ",
        "levels": {"L1": "L1-SYN-016", "L2": "L2-SYN-016", "L3": "L3-SYN-016"},
    },
]

_quick = {"pos": "ADJ", "class": "speed"}
LEXICON["quick"] = _quick
LEXICON["calm"] = _entry("ADJ", "quality")
LEXICON["silent"] = _entry("ADJ", "quality")
LEXICON["vet"] = _entry("NOUN", "person")
LEXICON["open"] = _entry("ADJ", "quality")
LEXICON["fast"] = {
    **_entry("ADV", "adverb"),
    "alternates": [{"pos": "ADJ", "class": "speed"}],
}

_INTRANSITIVE_VERBS = set(
    "ran run walked walk smiled smile clapped clap laughed laugh moved move fly ate "
    "eat waited wait danced dance jumped jump sat sit slept sleep stood stand talked "
    "talk looked look running smiling flying walking".split()
)
_DITRANSITIVE_VERBS = set(
    "gave give brought bring taught teach threw throw asked ask".split()
)
_VERB_SEMANTIC_GROUPS = {
    "motion": set(
        "ran run walked walk chased chase moved move boarded board flew fly jumping "
        "jump danced dance".split()
    ),
    "perception": set(
        "saw see heard hear observed observe noticed notice watched watch looked look".split()
    ),
    "communication": set("asked ask called call talked talk thanked thank".split()),
    "transfer": set(
        "gave give brought bring carried carry purchased purchase bought buy returned "
        "return placed place pulled pull pushed push threw throw".split()
    ),
    "creation": set(
        "painted paint cooked cook washed wash cleaned clean wrote write".split()
    ),
    "social": set(
        "smiled smile laughed laugh clapped clap played play helped help assisted assist".split()
    ),
    "cognition": set(
        "found find discovered discover started start began begin commenced commence "
        "waited wait wanted want needed need loved love".split()
    ),
    "consumption": set("ate eat".split()),
    "rest": set("sat sit slept sleep stood stand".split()),
    "physical": set("kicked kick".split()),
}
for _word, _info in LEXICON.items():
    if _info.get("pos", "").startswith("VERB_"):
        if _word in _INTRANSITIVE_VERBS:
            _info["frame"] = "INTRANSITIVE"
        elif _word in _DITRANSITIVE_VERBS:
            _info["frame"] = "DITRANSITIVE"
        else:
            _info["frame"] = "TRANSITIVE"
        for _semantic_class, _members in _VERB_SEMANTIC_GROUPS.items():
            if _word in _members:
                _info["class"] = _semantic_class
                break


STORIES: list[dict[str, Any]] = [
    {
        "id": "story_001",
        "title": "The Fox by the Bridge",
        "sentences": [
            ("Maya perceived a gigantic red fox near the ancient bridge.", ["Maya", "fox", "bridge"], "Maya sees a fox by a bridge."),
            ("The fox ran rapidly across the quiet road toward Maya.", ["fox", "road", "Maya"], "The fox runs toward Maya."),
            ("Maya followed the exhausted fox to a small shop.", ["Maya", "fox", "shop"], "Maya follows the fox to a shop."),
            ("The shopkeeper gave Maya a warm blanket and smiled.", ["shopkeeper", "Maya", "blanket"], "The shopkeeper gives Maya a blanket."),
        ],
    },
    {
        "id": "story_002",
        "title": "The Bird Under the Tree",
        "sentences": [
            ("Leo discovered a tiny blue bird beneath the tall tree.", ["Leo", "bird", "tree"], "Leo finds a bird under a tree."),
            ("The bird could not fly over the high wall.", ["bird", "wall"], "The bird cannot fly over the wall."),
            ("Leo carried the exhausted bird to the kind veterinarian.", ["Leo", "bird", "veterinarian"], "Leo carries the bird to the vet."),
            ("The veterinarian placed the bird inside a warm box.", ["veterinarian", "bird", "box"], "The vet puts the bird in a box."),
        ],
    },
    {
        "id": "story_003",
        "title": "The Drum Behind the House",
        "sentences": [
            ("Nina heard a loud drum behind the green house.", ["Nina", "drum", "house"], "Nina hears a drum behind a house."),
            ("She walked slowly toward the open gate.", ["Nina", "gate"], "Nina walks toward the gate."),
            ("A happy child commenced playing the drum.", ["child", "drum"], "A child starts to play the drum."),
            ("Nina clapped and the child smiled.", ["Nina", "child"], "Nina claps and the child smiles."),
        ],
    },
    {
        "id": "story_004",
        "title": "The Train at the Station",
        "sentences": [
            ("Omar observed a rapid train beside the old station.", ["Omar", "train", "station"], "Omar sees a train at the station."),
            ("The train carried a tired worker toward the city.", ["train", "worker", "city"], "The train carries a worker to the city."),
            ("Omar assisted the worker with a heavy bag.", ["Omar", "worker", "bag"], "Omar helps the worker with a bag."),
            ("The worker thanked Omar and boarded the train.", ["worker", "Omar", "train"], "The worker thanks Omar and boards the train."),
        ],
    },
    {
        "id": "story_005",
        "title": "The Dog at the Market",
        "sentences": [
            ("Ava found an enormous brown dog near the busy market.", ["Ava", "dog", "market"], "Ava finds a dog at the market."),
            ("The dog was hungry and the market was loud.", ["dog", "market"], "The dog is hungry and the market is loud."),
            ("Ava purchased some food for the dog.", ["Ava", "food", "dog"], "Ava buys food for the dog."),
            ("The happy dog ate and Ava smiled.", ["dog", "Ava"], "The dog eats and Ava smiles."),
        ],
    },
    {
        "id": "story_006",
        "title": "The Kite Above the Hill",
        "sentences": [
            ("Ben saw a bright kite above the high hill.", ["Ben", "kite", "hill"], "Ben sees a kite above a hill."),
            ("The kite moved quickly across the gray sky.", ["kite", "sky"], "The kite moves across the sky."),
            ("Ben chased the kite down the long road.", ["Ben", "kite", "road"], "Ben chases the kite down a road."),
            ("A kind farmer returned the kite and Ben laughed.", ["farmer", "kite", "Ben"], "A farmer returns the kite and Ben laughs."),
        ],
    },
]


MODEL_CORPUS = [
    "Maya saw a big red fox near the old bridge.",
    "The fox ran fast across the calm road toward Maya.",
    "Maya followed the tired fox to a small shop.",
    "The shopkeeper gave Maya a soft blanket and smiled.",
    "Leo found a small blue bird under the tall tree.",
    "The bird could not fly over the high wall.",
    "Leo carried the tired bird to the kind vet.",
    "The vet placed the bird inside a warm box.",
    "Nina heard a loud drum behind the green house.",
    "She walked slowly toward the open gate.",
    "A happy child started to play the drum.",
    "Nina clapped and the child smiled.",
    "Omar saw a fast train beside the old station.",
    "The train carried a tired worker toward the city.",
    "Omar helped the worker with a heavy bag.",
    "The worker thanked Omar and boarded the train.",
    "Ava found a big brown dog near the busy market.",
    "The dog was hungry and the market was loud.",
    "Ava bought some food for the dog.",
    "The happy dog ate and Ava smiled.",
    "Ben saw a bright kite above the high hill.",
    "The kite moved quickly across the dark sky.",
    "Ben chased the kite down the long road.",
    "A kind farmer returned the kite and Ben laughed.",
    "A big dog ran fast.",
    "A small bird could not fly.",
    "The vet helped the dog.",
    "The child played and the dog smiled.",
    "The train moved toward the city.",
    "The farmer found a kite on the hill.",
]

ENTITY_ALIASES = {
    "veterinarian": ["veterinarian", "vet"],
    "Nina": ["Nina", "she"],
}


LEVEL_DEFINITIONS = [
    (
        "L1",
        "Emerging readers",
        8,
        {
            "perceived",
            "gigantic",
            "ancient",
            "rapidly",
            "quiet",
            "exhausted",
            "discovered",
            "beneath",
            "veterinarian",
            "commenced",
            "observed",
            "rapid",
            "assisted",
            "purchased",
            "enormous",
            "gray",
        },
    ),
    (
        "L2",
        "Developing readers",
        11,
        {
            "perceived",
            "gigantic",
            "veterinarian",
            "commenced",
            "purchased",
            "enormous",
            "beneath",
        },
    ),
    ("L3", "Independent readers", 14, set()),
]


def _stable_seed(*parts: object) -> int:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _build_story(raw: dict[str, Any], index: int, seed: int) -> dict[str, Any]:
    story_id = raw["id"]
    exercise_seed = _stable_seed(seed, story_id, "exercise") % 1_000_000_007
    sentence_records: list[dict[str, Any]] = []
    event_records: list[dict[str, Any]] = []
    for event_order, (text, entities, summary) in enumerate(raw["sentences"], start=1):
        sentence_id = f"{story_id}-S{event_order:02d}"
        event_id = f"{story_id}-E{event_order:02d}"
        sentence_record = {
            "sentence_id": sentence_id,
            "event_id": event_id,
            "event_order": event_order,
            "text": text,
            "key_entities": list(entities),
            "word_count": word_count(text),
        }
        sentence_records.append(sentence_record)
        event_records.append(
            {
                "event_id": event_id,
                "event_order": event_order,
                "sentence_id": sentence_id,
                "key_entities": list(entities),
                "summary": summary,
            }
        )
    return {
        "story_id": story_id,
        "title": raw["title"],
        "exercise_seed": exercise_seed,
        "sentences": sentence_records,
        "events": event_records,
        "critical_entities": sorted(
            {
                entity
                for sentence in sentence_records
                for entity in sentence["key_entities"]
            },
            key=str.lower,
        ),
    }


def _build_levels() -> list[dict[str, Any]]:
    all_words = sorted(LEXICON)
    levels: list[dict[str, Any]] = []
    for level_id, name, max_words, excluded in LEVEL_DEFINITIONS:
        allowed = [word for word in all_words if word not in excluded]
        synonyms = []
        for entry in SYNONYM_TABLE:
            if level_id not in entry["levels"]:
                continue
            targets = []
            for target, target_id in entry["options"]:
                if target not in allowed:
                    continue
                targets.append(
                    {
                        "to": target,
                        "entry_ref": entry["levels"][level_id],
                        "target_ref": f"{level_id}-VOCAB-{target.upper()}",
                        "pos": LEXICON[target]["pos"],
                    }
                )
            if not targets:
                continue
            synonyms.append(
                {
                    "from": entry["source"],
                    "pos": entry["pos"],
                    "options": targets,
                }
            )
        levels.append(
            {
                "level_id": level_id,
                "name": name,
                "max_words": max_words,
                "allowed_words": allowed,
                "allowed_word_count": len(allowed),
                "synonyms": synonyms,
                "modifier_policy": {
                    "deletable_pos": ["ADJ", "ADV"],
                    "delete_only_before_noun": True,
                    "delete_only_after_verb_for_adverbs": True,
                    "rule_ref": f"{level_id}-MOD-DEL",
                },
                "split_policy": {
                    "conjunctions": ["and", "but", "so", "because", "then"],
                    "min_words_per_part": 2,
                    "require_right_clause_subject": True,
                    "rule_ref": f"{level_id}-SPLIT-CONJ",
                },
                "constraint_refs": {
                    "max_words": f"{level_id}-LEN",
                    "vocabulary": f"{level_id}-VOCAB",
                },
            }
        )
    return levels


def build_data(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    rng = random.Random(_stable_seed(seed, "story-order"))
    story_order = list(STORIES)
    rng.shuffle(story_order)
    stories = [_build_story(raw, index, seed) for index, raw in enumerate(story_order)]
    return {
        "schema_version": 1,
        "generator": {
            "name": GENERATOR_NAME,
            "seed": seed,
            "algorithm": "sha256-seeded-python-random",
        },
        "stories": stories,
        "levels": _build_levels(),
        "lexicon": {word: LEXICON[word] for word in sorted(LEXICON)},
        "model_corpus": MODEL_CORPUS,
        "entity_aliases": ENTITY_ALIASES,
    }


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def generate_workspace(data_dir: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    data = build_data(seed)
    _json_dump(
        data_dir / "stories.json",
        {
            "schema_version": data["schema_version"],
            "generator": data["generator"],
            "entity_aliases": ENTITY_ALIASES,
            "stories": data["stories"],
        },
    )
    _json_dump(
        data_dir / "grading_levels.json",
        {
            "schema_version": data["schema_version"],
            "generator": data["generator"],
            "levels": data["levels"],
        },
    )
    _json_dump(
        data_dir / "lexicon.json",
        {
            "schema_version": data["schema_version"],
            "generator": data["generator"],
            "lexicon": data["lexicon"],
        },
    )
    _json_dump(
        data_dir / "model_corpus.json",
        {
            "schema_version": data["schema_version"],
            "generator": data["generator"],
            "sentences": data["model_corpus"],
        },
    )
    manifest = {
        "schema_version": data["schema_version"],
        "generator": data["generator"],
        "story_count": len(data["stories"]),
        "level_count": len(data["levels"]),
        "files": [
            "grading_levels.json",
            "lexicon.json",
            "model_corpus.json",
            "stories.json",
        ],
        "stories": [
            {
                "story_id": story["story_id"],
                "title": story["title"],
                "sentence_count": len(story["sentences"]),
                "exercise_seed": story["exercise_seed"],
            }
            for story in data["stories"]
        ],
    }
    _json_dump(data_dir / "generation_manifest.json", manifest)
    return manifest


def load_data(data_dir: str | Path) -> dict[str, Any]:
    data_dir = Path(data_dir)
    stories_doc = json.loads((data_dir / "stories.json").read_text(encoding="utf-8"))
    levels_doc = json.loads((data_dir / "grading_levels.json").read_text(encoding="utf-8"))
    lexicon_doc = json.loads((data_dir / "lexicon.json").read_text(encoding="utf-8"))
    corpus_doc = json.loads((data_dir / "model_corpus.json").read_text(encoding="utf-8"))
    return {
        "schema_version": stories_doc["schema_version"],
        "generator": stories_doc["generator"],
        "entity_aliases": stories_doc.get("entity_aliases", {}),
        "stories": stories_doc["stories"],
        "levels": levels_doc["levels"],
        "lexicon": lexicon_doc["lexicon"],
        "model_corpus": corpus_doc["sentences"],
    }


def story_text(story: dict[str, Any]) -> str:
    return " ".join(sentence["text"] for sentence in story["sentences"])


def story_word_stream(story: dict[str, Any]) -> list[str]:
    return [token for sentence in story["sentences"] for token in tokenize(sentence["text"])]


def body_tokens(text: str) -> list[str]:
    return tokenize(text)


def render_model_sentence(text: str) -> str:
    return detokenize(body_tokens(text))
