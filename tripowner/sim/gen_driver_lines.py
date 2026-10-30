"""Generate varied, natural driver lines with Nemotron and save them for reproducible evals.

    python -m tripowner.sim.gen_driver_lines --per 8

Two separate model calls per (intent, language): one writes the lines in the driver's language,
another translates them to English, the way the runtime voice layer would. The output is
committed, so evals with `--driver pool` cost nothing to re-run and every agent hears the same lines.

The committed pool was also reviewed by hand: 13 lines (mostly Kannada) whose English meaning was wrong or
nonsensical were removed. Re-running this script regenerates the pool from scratch and needs the same review.
"""
import argparse
import asyncio
import json
from pathlib import Path

from ..driver import INTENTS
from ..i18n import LANG_NAMES
from ..llm import get_llm
from ..providers.voice import clean_script

OUT = Path(__file__).with_name("driver_lines.json")
LANGS = ("kn-IN", "hi-IN", "en-IN")
USED_INTENTS = ("where", "phone", "coming", "arrived")


async def _retry(fn, *args, attempts: int = 3):
    """Token Factory occasionally drops a connection or a reasoning model returns no JSON."""
    for i in range(attempts):
        try:
            return await fn(*args)
        except Exception as e:  # network error, bad JSON, wrong length
            if i == attempts - 1:
                raise
            print(f"  retry {i + 1} after: {type(e).__name__}: {str(e)[:80]}")
            await asyncio.sleep(2)


def _json_list(text: str | None) -> list[str]:
    text = text or ""
    start, end = text.find("["), text.rfind("]")
    items = json.loads(text[start:end + 1])
    return [str(x).strip() for x in items if str(x).strip()]


async def _lines(llm, intent: str, lang: str, n: int) -> list[str]:
    if lang == "en-IN":
        style = ("casual Indian English, written only in the Latin alphabet "
                 "(an occasional Hindi or Kannada word in Latin letters is fine)")
    else:
        style = (f"casual, everyday {LANG_NAMES[lang]}, written only in {LANG_NAMES[lang]} script "
                 f"(English words are fine, in Latin letters)")
    msg = await llm.chat([
        {"role": "system", "content": (
            f"You write realistic messages from Bengaluru cab drivers to their riders, in {style}. "
            f"Vary the wording a lot: some short, some indirect, some with local slang. "
            f"Output only a JSON array of {n} strings."
        )},
        {"role": "user", "content": f"Each message should do this: {INTENTS[intent]}"},
    ], temperature=0.9, max_tokens=6000)  # reasoning tokens count against the budget
    lines = [t for t in _json_list(msg["content"]) if clean_script(t, lang)]
    if not lines:
        raise ValueError("no usable lines (garbled script)")
    return lines


async def _translate(llm, lines: list[str], lang: str) -> list[str]:
    # en-IN lines are translated too: drivers mix Hindi/Kannada words in, and the agent works in plain English.
    msg = await llm.chat([
        {"role": "system", "content": (
            f"Translate each {LANG_NAMES[lang]} (possibly mixed-language) message to plain English, keeping the meaning and tone. "
            f"Output only a JSON array of strings, same order and length as the input."
        )},
        {"role": "user", "content": json.dumps(lines, ensure_ascii=False)},
    ], temperature=0.0, max_tokens=6000)
    out = _json_list(msg["content"])
    if len(out) != len(lines):
        raise ValueError(f"translation returned {len(out)} lines for {len(lines)}")
    return out


async def main(per: int) -> None:
    llm = get_llm()
    if llm is None:
        raise SystemExit("Set LLM_BASE_URL and LLM_API_KEY in .env first")
    # Resume: keep whatever a previous (interrupted) run already saved.
    pool: dict[str, dict[str, list[dict]]] = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for intent in USED_INTENTS:
        pool.setdefault(intent, {})
        for lang in LANGS:
            if len(pool[intent].get(lang, [])) >= per:
                continue
            pairs: list[dict] = []
            for _ in range(4):   # garbled lines and bad translations are dropped; top up until there are enough
                seen = {p["text"] for p in pairs}
                native = [t for t in await _retry(_lines, llm, intent, lang, per) if t not in seen]
                english = await _retry(_translate, llm, native, lang)
                pairs += [{"text": t, "en": e} for t, e in zip(native, english) if clean_script(e, "en-IN")]
                if len(pairs) >= per:
                    break
            pool[intent][lang] = pairs[:per]
            OUT.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{intent:8} {lang}: {len(pool[intent][lang])} lines")
    print(f"-> {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=8)
    asyncio.run(main(ap.parse_args().per))
