#!/usr/bin/env python3
"""Fast OpenRouter Responses API cache probe.

Sends the same large system prefix twice with cache_control + session_id and
prints cached_tokens from the second call. Exit nonzero if the second call has
zero cached tokens.
"""

from __future__ import annotations

import argparse
import os
import sys

from openai import OpenAI


def usage_cached(response) -> tuple[int, int, int]:
    usage = response.usage
    if usage is None:
        return 0, 0, 0
    details = getattr(usage, "input_tokens_details", None) or getattr(
        usage, "prompt_tokens_details", None
    )
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
    inp = int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0)
    out = int(
        getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0
    )
    return cached, inp, out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--chars", type=int, default=24000)
    parser.add_argument("--session-id", default="wr-cache-probe")
    args = parser.parse_args()

    client = OpenAI()
    filler = ("trajectory analysis cache probe " * 400)[: args.chars]
    body = {
        "model": args.model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are a trajectory analyst. Keep this long stable prefix "
                    "identical across turns.\n\n" + filler
                ),
            },
            {"role": "user", "content": "Reply with OK only."},
        ],
        "max_output_tokens": 16,
        "extra_body": {
            "cache_control": {"type": "ephemeral"},
            "session_id": args.session_id,
        },
        "extra_headers": {"x-session-id": args.session_id},
    }

    first = client.responses.create(**body)
    c1, i1, o1 = usage_cached(first)
    body["input"][1]["content"] = "Reply with OK again."
    second = client.responses.create(**body)
    c2, i2, o2 = usage_cached(second)

    print(
        {
            "model": args.model,
            "first": {"cached": c1, "input": i1, "output": o1},
            "second": {"cached": c2, "input": i2, "output": o2},
            "cache_hit": c2 > 0,
            "base_url": os.environ.get("OPENAI_BASE_URL"),
        }
    )
    return 0 if c2 > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
