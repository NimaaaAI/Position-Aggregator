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

# Rough ordering, best first, used only to sort the listing so the useful models
# are not buried among dozens of embedding and audio ones. Anything not named
# here still gets listed, just underneath.
PREFERRED = [
    "gpt-5", "o3", "gpt-4.1", "gpt-4o", "o4-mini", "gpt-4.1-mini",
    "gpt-4o-mini", "o3-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo",
]


def client():
    if not API_KEY:
        sys.exit(
            "LLM_API_KEY is not set.\n"
            "  cp .env.example .env    then paste your key into it"
        )
    from openai import OpenAI

    return OpenAI(base_url=BASE_URL, api_key=API_KEY)


def rank_of(name):
    """Where a model sits in PREFERRED, or at the end if it is not listed."""
    for index, preferred in enumerate(PREFERRED):
        if name.startswith(preferred):
            return index
    return len(PREFERRED)


def list_models():
    print(f"asking {BASE_URL} what this key can reach\n")
    try:
        models = client().models.list()
    except Exception as error:
        sys.exit(f"could not reach the API: {error}")

    names = sorted(model.id for model in models.data)
    if not names:
        sys.exit("the API returned no models")

    # Chat models first, in rough strength order. The rest -- embeddings, audio,
    # image -- are listed separately because they cannot answer a question.
    chat = sorted(
        [n for n in names if rank_of(n) < len(PREFERRED)],
        key=lambda n: (rank_of(n), n),
    )
    other = [n for n in names if rank_of(n) == len(PREFERRED)]

    print(f"{len(names)} model(s) available\n")
    print("--- chat models, strongest first")
    for name in chat:
        marker = "  <- default (LLM_MODEL in .env)" if name == MODEL else ""
        print(f"  {name}{marker}")

    if other:
        print(f"\n--- everything else ({len(other)})")
        for name in other:
            print(f"  {name}")


parser = argparse.ArgumentParser()
parser.add_argument("--models", action="store_true",
                    help="list the models this API key can reach")
args = parser.parse_args()

if args.models:
    list_models()
else:
    sys.exit("nothing to do yet -- try: python ask.py --models")
