"""Ask questions about the positions, with a language model writing the answer.

    python ask.py --models     list the models the API key can reach

Retrieval and reranking run locally and cost nothing. Only the final answer is
sent to the API, and only ever a handful of positions -- all 1,951 adverts would
be roughly 4.7 million tokens.
"""

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("LLM_BASE_URL", "https://api.gapgpt.app/v1")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

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
# chosen the positions; the model only has to summarise eight of them and quote
# the links, which does not need a frontier model.
CHEAP = ("gpt-4o-mini", "gpt-5-nano", "gpt-5.4-nano", "gemini-2.5-flash-lite",
         "gemini-3.1-flash-lite", "deepseek-chat", "gpt-4.1-nano")


def client():
    if not API_KEY:
        sys.exit(
            "LLM_API_KEY is not set.\n"
            "  cp .env.example .env    then paste your key into it"
        )
    from openai import OpenAI

    return OpenAI(base_url=BASE_URL, api_key=API_KEY)


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


parser = argparse.ArgumentParser()
parser.add_argument("--models", action="store_true",
                    help="list the models this API key can reach")
args = parser.parse_args()

if args.models:
    list_models()
else:
    sys.exit("nothing to do yet -- try: python ask.py --models")
