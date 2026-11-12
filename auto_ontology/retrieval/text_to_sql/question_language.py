# SPDX-License-Identifier: Apache-2.0

"""Let users ask in any language while the ontology stays English.

The ontology (Terms, definitions, SqlAttributes, labels) is English only, and
the embedding model is English-centred, so a French question retrieves English
Terms worse than its English equivalent. With ``QUESTION_TRANSLATION_ENABLED``
on (default off), a non-English question is translated once to English; that
English text drives all retrieval, SQL generation sees both the user's own
words and the translation, and the prose answer is written in the user's
language. SQL, table and column names are never translated.

Everything here is best effort: any failure logs a warning and the original
question is used unchanged. With the flag off nothing in this module changes
any prompt or state.
"""

from __future__ import annotations

import json
import logging
import os
import re

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

ENABLED_ENV = "QUESTION_TRANSLATION_ENABLED"
MAX_TRANSLATION_TOKENS = 400

# path_state keys.
KEY_LANGUAGE = "question_language"
KEY_ENGLISH = "question_english"
KEY_TRANSLATED = "question_translated"

TRANSLATION_PROMPT = """Detect the language of the user's question and translate it into English.

Return ONLY a JSON object: {{"language": "<ISO 639-1 code of the question's language>", "english": "<the question in English>"}}

Rules:
- Translate faithfully. Never add interpretation, explanation or extra detail, and never drop any.
- Keep quoted strings, proper nouns, numbers and dates exactly as written.
- Keep anything that looks like a stored data value (people's names, titles, places, product or company names) exactly as written, untranslated.
- If the question is already English, return the language code "en" and the question unchanged.

Question:
{question}"""

_LANGUAGE_NAMES = {
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ar": "Arabic",
    "zh": "Chinese",
    "ja": "Japanese",
    "ru": "Russian",
}

# Words that mark a question as English or as another European language.
# Words that are also common English words ("a", "me", "on", "an") are left out
# of the foreign set. This only gates the model call: a miss costs one cheap
# call, never a wrong answer.
_ENGLISH_WORDS = frozenset(
    "the of and is are was were what which who whom how many much show list "
    "give me top for with by from in on at to all each per total number "
    "average count customers most least highest lowest than that this "
    "have has had does did there their where when why between".split()
)
_FOREIGN_WORDS = frozenset(
    (
        # French
        "le la les des du de un une est sont quels quelles quel quelle combien "
        "pour par avec dans sur et ou qui que quoi nombre moyenne chaque tous "
        "toutes montre donne liste plus moins entre ont mes "
        # Spanish
        "el los las del es son cuantos cuantas cual cuales por con para y "
        "donde cuanto muestra dame lista "
        # German
        "der die das und ist sind wie viele welche mit von im zeige gib "
        "alle jeden pro zwischen "
        # Italian / Portuguese
        "il lo gli quanti quante qual quali sono quantos quantas uma nao"
    ).split()
)
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def question_translation_enabled() -> bool:
    """Whether question translation is on (``QUESTION_TRANSLATION_ENABLED``, default off)."""
    return os.environ.get(ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class QuestionLanguage:
    """The language of a question and its English form."""

    language: str
    english: str
    translated: bool


def looks_english(question: str) -> bool:
    """Cheap deterministic check: True only when the question is confidently English.

    Plain ASCII letters and no foreign stopword (or clearly more English than
    foreign stopwords). Anything else is "unsure" and goes to the model.
    """
    text = question.strip()
    if not text:
        return True
    if any(ch.isalpha() and ord(ch) > 127 for ch in text):
        return False
    words = [w.lower() for w in _WORD_RE.findall(text)]
    foreign = sum(1 for w in words if w in _FOREIGN_WORDS)
    if foreign == 0:
        return True
    english = sum(1 for w in words if w in _ENGLISH_WORDS)
    return english > 2 * foreign


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content
            if isinstance(part, (str, dict))
        )
    return str(content or "")


def parse_translation(raw: str, original: str) -> QuestionLanguage | None:
    """Parse the model's JSON reply; None when it is malformed or implausible."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    language = data.get("language")
    english = data.get("english")
    if not isinstance(language, str) or not isinstance(english, str):
        return None
    language = language.strip().lower()
    english = " ".join(english.split())
    if not re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})?", language) or not english:
        return None
    # A translation far longer than its source is the model explaining.
    if len(english) > 3 * len(original) + 200:
        return None
    if language.split("-")[0] == "en":
        return QuestionLanguage("en", original, False)
    return QuestionLanguage(language, english, english != original.strip())


def resolve_question_language(question: str, llm: Any) -> QuestionLanguage | None:
    """Language and English form of *question*, or None when unavailable.

    Never raises. None means "carry on with the original question".
    """
    try:
        if not question.strip():
            return None
        if looks_english(question):
            return QuestionLanguage("en", question, False)
        if llm is None:
            logger.warning("Question translation skipped: no LLM client")
            return None
        try:
            bound = llm.bind(max_tokens=MAX_TRANSLATION_TOKENS)
        except Exception:  # noqa: BLE001 - a client without bind is called as is
            bound = llm
        reply = bound.invoke(
            [HumanMessage(content=TRANSLATION_PROMPT.format(question=question))]
        )
        result = parse_translation(
            _content_text(getattr(reply, "content", "")), question
        )
        if result is None:
            logger.warning("Question translation returned unusable output; ignoring it")
            return None
        if result.translated:
            logger.info(
                "Question translated: language=%s original=%r english=%r",
                result.language,
                question,
                result.english,
            )
        return result
    except Exception:  # noqa: BLE001 - translation must never break an answer
        logger.warning("Question translation failed; using the original", exc_info=True)
        return None


def record_in_path_state(path_state: dict, result: QuestionLanguage) -> None:
    """Store the outcome where later nodes and the final answer read it."""
    path_state[KEY_LANGUAGE] = result.language
    path_state[KEY_ENGLISH] = result.english
    path_state[KEY_TRANSLATED] = result.translated


def is_translated(path_state: dict | None) -> bool:
    return bool((path_state or {}).get(KEY_TRANSLATED))


def language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get(code.split("-")[0], code)


def language_instruction(path_state: dict | None) -> str:
    """System note for generation; "" unless the question was translated."""
    if not is_translated(path_state):
        return ""
    code = str((path_state or {}).get(KEY_LANGUAGE) or "")
    name = language_name(code)
    return (
        f"The user wrote their question in {name}. 'Original user request' is the "
        "user's own wording; 'Normalized retrieval question' is a machine "
        "translation to English used only for searching the English ontology. "
        "If they differ in meaning, the user's own wording wins. Keep data values "
        "(names, titles, places) exactly as the user wrote them. "
        f"Write the prose answer (the response text) in {name}. SQL, table names "
        "and column names stay exactly as in the database."
    )


def answer_fields(path_state: dict | None) -> dict[str, str]:
    """``question_language`` / ``question_english`` for the final answer, or {}."""
    ps = path_state or {}
    if not ps.get(KEY_LANGUAGE):
        return {}
    return {
        "question_language": str(ps[KEY_LANGUAGE]),
        "question_english": str(ps.get(KEY_ENGLISH) or ""),
    }
