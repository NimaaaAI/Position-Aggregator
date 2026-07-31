"""Ask questions about the positions, with a language model writing the answer.

    python ask.py "funded PhD in machine learning in the Netherlands"
    python ask.py "which quantum computing positions close soonest?" --context 15
    python ask.py --models

Retrieval and reranking run locally and cost nothing. Only the final answer goes to
the API, and only a handful of positions with it -- all 1,951 adverts would be
roughly 4.7 million tokens.

Two separate numbers, deliberately:

    --show      how many positions are printed for you.  Default 40.
    --context   how many the model is given to write about.  Default 10.

Nothing is hidden. The model summarises the head of a list you can read in full, so
a position ranked eleventh is still on your screen even if no paragraph mentions it.
"""

import argparse
import os
import re
import sys
import textwrap

from dotenv import load_dotenv

from search import describe, retrieve

load_dotenv()

BASE_URL = os.getenv("LLM_BASE_URL", "https://api.gapgpt.app/v1")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

# Seconds to wait for an answer. Settable in .env for a slow link.
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "180"))

# How much of each advert the model sees. The reranker has already decided these
# are the right positions; the model only has to describe them.
CONTEXT_CHARS = 600

SYSTEM = """You help someone find academic jobs.

You are given positions already selected from a database by a search system, strongest
first. Use only those. Never invent a position or a deadline.

Account for every position you are given. If you are handed ten, your answer mentions
all ten, each exactly once, by its number. This is not a preference -- a position you
leave out is one the reader will never see, and they cannot judge what they are not
shown.

Order them by how well they fit, best first, and group them if that helps to read:
a close match, a partial one, something adjacent, and at the end the ones that do not
really fit. For each, one or two lines: what it is, and how it relates to the question.
For a poor fit, say plainly that it is a poor fit and why -- one line is enough. Never
silently omit it.

Someone asking about AI wants to hear about every position involving AI, in any field,
and will decide for themselves whether AI in robotics or AI in biology suits them.
Someone asking about medical imaging wants the imaging work that has no AI in it too.

You are describing what the search found. Deciding what is worth their time is their
job, not yours.

Refer to positions by their number, like [3]. Quote the closing date when it matters.

Never write out a URL. They are added afterwards from the database, where they are
correct.

If nothing in the list fits, say so plainly rather than stretching to make something
look relevant."""

# Where the model cited a position, like [3] or [12].
CITED = re.compile(r"\[(\d+)\]")

# Models that cannot answer a question, whatever their name suggests. Matched as
# substrings. "gpt-4o-mini-tts" is text-to-speech, not a smaller gpt-4o-mini, and
# would otherwise sit near the top of the list looking like a sensible choice.
NOT_CHAT = (
    "embedding", "tts", "whisper", "dall-e", "imagen", "flux",
    "-image", "image-", "z-image", "audio", "-live-",
)

# Families in rough capability order, used only to group the listing. Within a
# family the API's own naming does the sorting.
FAMILIES = [
    ("OpenAI",   ("gpt-", "o3", "o4", "chatgpt-")),
    ("Claude",   ("claude-",)),
    ("Gemini",   ("gemini-", "gemma-")),
    ("Grok",     ("grok-",)),
    ("DeepSeek", ("deepseek-",)),
    ("Qwen",     ("qwen", "Qwen", "gapgpt-qwen")),
    ("Other",    ("kimi", "glm-")),
]

# Good enough for this job and cheap. The retrieval and reranking have already
# chosen the positions; the model only has to summarise them and quote the links,
# which does not need a frontier model.
CHEAP = ("gpt-4o-mini", "gpt-5-nano", "gpt-5.4-nano", "gemini-2.5-flash-lite",
         "gemini-3.1-flash-lite", "deepseek-chat", "gpt-4.1-nano")


def client():
    if not API_KEY:
        sys.exit(
            "LLM_API_KEY is not set.\n"
            "  cp .env.example .env    then paste your key into it"
        )
    from openai import OpenAI

    # Generous, and retried. Reasoning models spend a lot of hidden tokens before
    # producing anything -- gpt-5-nano used 1,400 output tokens on a three-line
    # answer -- and this runs over a slow link through a proxy.
    return OpenAI(
        base_url=BASE_URL, api_key=API_KEY, timeout=TIMEOUT, max_retries=2,
    )


def is_chat(name):
    return not any(marker in name for marker in NOT_CHAT)


def family_of(name):
    for label, prefixes in FAMILIES:
        if any(name.startswith(prefix) for prefix in prefixes):
            return label
    return "Other"


def list_models():
    print(f"asking {BASE_URL} what this key can reach\n")
    try:
        models = client().models.list()
    except Exception as error:
        sys.exit(f"could not reach the API: {error}")

    names = sorted(model.id for model in models.data)
    if not names:
        sys.exit("the API returned no models")

    chat = [name for name in names if is_chat(name)]
    other = [name for name in names if not is_chat(name)]

    print(f"{len(names)} model(s): {len(chat)} can answer questions, "
          f"{len(other)} cannot\n")

    for label, _ in FAMILIES:
        group = [name for name in chat if family_of(name) == label]
        if not group:
            continue
        print(f"--- {label}")
        for name in group:
            notes = []
            if name == MODEL:
                notes.append("default")
            if name in CHEAP:
                notes.append("cheap, enough for this")
            suffix = f"   <- {', '.join(notes)}" if notes else ""
            print(f"  {name}{suffix}")
        print()

    print(f"--- not usable here: embedding, speech and image models ({len(other)})")
    print(f"  {', '.join(other[:8])}, ...")


def as_context(results):
    """The positions as the model sees them: numbered, with the facts it must not
    invent, and enough of the advert to describe what the work is."""
    blocks = []
    for number, item in enumerate(results, 1):
        where = ", ".join(p for p in (item["city"], item["country"]) if p) or "not given"
        closes = item["closes_at"]
        blocks.append("\n".join([
            f"[{number}] {item['title']}",
            f"employer: {item['employer']}",
            f"location: {where}",
            f"closes: {closes:%d %B %Y}" if closes else "closes: not given",
            f"url: {item['url']}",
            f"advert: {' '.join((item['description'] or '').split())[:CONTEXT_CHARS]}",
        ]))
    return "\n\n".join(blocks)


def with_urls(text, results):
    """Put the real URL next to each citation the model made.

    Done here rather than by the model. A model does not copy text, it regenerates
    it token by token, so a long slug comes back subtly altered: asked to repeat a
    URL it once produced ".../abdominal-aneurysm/251224" for
    ".../abdominal-aortic-aneurysm/251224" -- a confident, broken link that looks
    exactly like a working one.

    The model supplies the reference, the database supplies the address.
    """
    seen = set()

    def replace(match):
        number = int(match.group(1))
        if not 1 <= number <= len(results) or number in seen:
            return match.group(0)
        seen.add(number)
        return f"{match.group(0)} {results[number - 1]['url']}"

    return CITED.sub(replace, text)


def answer(question, results, model):
    # The count is stated rather than left to be inferred. "Mention all of them" is
    # a suggestion a model will quietly round down; "all 10, numbered [1] to [10]"
    # is a requirement it can check itself against.
    prompt = (
        f"Question: {question}\n\n"
        f"There are {len(results)} positions below, numbered [1] to [{len(results)}].\n"
        f"Your answer must mention all {len(results)} of them, each exactly once.\n\n"
        f"{as_context(results)}"
    )
    try:
        response = client().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            # Zero, not low. There is no writing to be done here, only a fixed list
            # to be described, and the same question should give the same answer
            # twice. At 0.2 this query returned three positions, then two, then one.
            temperature=0,
        )
    except Exception as error:
        if "timed out" in str(error).lower() or "timeout" in type(error).__name__.lower():
            sys.exit(
                f"the model did not answer within {TIMEOUT:.0f}s (tried 3 times).\n"
                f"  reasoning models such as gpt-5-nano are slow here -- they spend\n"
                f"  hundreds of hidden tokens before writing anything. Try:\n"
                f"    --model gpt-4o-mini      much faster for this job\n"
                f"    --context 5              less for it to read\n"
                f"  or raise LLM_TIMEOUT in .env"
            )
        sys.exit(f"the API call failed: {error}")

    return response.choices[0].message.content, getattr(response, "usage", None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", help="what you are looking for")
    parser.add_argument("--models", action="store_true",
                        help="list the models this API key can reach")
    parser.add_argument("--show", type=int, default=40,
                        help="how many positions to print (default 40)")
    parser.add_argument("--context", type=int, default=10,
                        help="how many the model is given (default 10)")
    parser.add_argument("--model", default=MODEL, help=f"which model (default {MODEL})")
    parser.add_argument("--open", action="store_true", dest="open_only",
                        help="only positions whose closing date has not passed")
    parser.add_argument("--no-rerank", action="store_true",
                        help="skip the cross-encoder; faster, less accurate")
    args = parser.parse_args()

    if args.models:
        list_models()
        return
    if not args.question:
        sys.exit('ask something, e.g.  python ask.py "funded PhD in robotics"')

    results = retrieve(
        args.question, limit=args.show, open_only=args.open_only,
        rerank=not args.no_rerank,
    )
    if not results:
        sys.exit("nothing found")

    given = results[:args.context]
    print(f"\nretrieved {len(results)}, giving {len(given)} to {args.model}\n")

    text, usage = answer(args.question, given, args.model)
    text = with_urls(text, given)

    print("=" * 88)
    for line in text.splitlines():
        print("\n".join(textwrap.wrap(line, width=88)) if line.strip() else "")
    print("=" * 88)

    # Which of the positions the model actually wrote about. It is told to cover all
    # of them; this is how we find out whether it did, rather than counting by eye.
    mentioned = {int(n) for n in CITED.findall(text)}
    missing = [n for n in range(1, len(given) + 1) if n not in mentioned]
    if missing:
        print(f"\nthe model left out {len(missing)} of {len(given)} positions it was "
              f"given: {', '.join(f'[{n}]' for n in missing)}")
        print("they are in the list below, just without a comment on them")

    if usage:
        print(f"\ntokens: {usage.prompt_tokens} in, {usage.completion_tokens} out")

    # Everything retrieved, not just what the model was shown. A position ranked
    # eleventh may be the one you actually want.
    print(f"\nall {len(results)} positions found:\n")
    for place, item in enumerate(results, 1):
        marker = " *" if place <= len(given) else "  "
        first, *rest = describe(item)
        print(f"{marker}{place:>3}. {first}")
        for line in rest[:2]:
            print(f"       {line}")
        print()
    print("* = shown to the model")


if __name__ == "__main__":
    main()
