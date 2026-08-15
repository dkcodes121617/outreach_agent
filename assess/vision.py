"""Vision over the PageSpeed screenshot — "current or dated, and why".

Called **only when the deterministic signals are ambiguous**. A site serving
HTTP with no viewport tag and a 2018 footer needs no model to judge it; a site
that is technically fine but simply looks like 2014 is exactly what a model sees
and a fingerprint cannot.

## Currently disabled, and this was measured rather than assumed

ARCHITECTURE §14 flagged Groq's vision lineup as the thing most likely to have
moved. It has, and further than expected. Checked against the live account on
7 Aug 2026:

  - `VISION_MODEL`'s configured default,
    `meta-llama/llama-4-scout-17b-16e-instruct`, returns
    **404 model_not_found**.
  - `GET /openai/v1/models` lists **15 models and not one is multimodal** —
    they are text, audio (whisper/orpheus) and prompt-guard models.
  - The LLMsRelay proxy is not a fallback either: a Messages request carrying
    an `image` content block returns HTTP 200, but the model replies "I don't
    see any image attached". **The proxy silently strips image blocks.**

So `VISION_MODEL` is left **blank**, which disables this pass. That is a real
capability loss and it is worth being precise about what it costs: `vision_dated`
is 18 of the ~140 points available in `score.py`, and every other signal is
deterministic. The assessment works without it; it is simply less able to
recognise a site that is technically healthy but visually stale.

Nothing here needs to change to switch it back on — set `VISION_MODEL` to a
multimodal model on a provider that serves one, and this runs again.
"""
from __future__ import annotations

import base64
import json
import logging

log = logging.getLogger("outreach.assess.vision")

_SYSTEM = """\
You are looking at a screenshot of the top of a small business's website.

Judge how it would read to a prospective customer visiting on a phone today. \
Design era matters more than taste: layouts, typography and imagery date in \
recognisable ways.

Answer with a JSON object:
{"verdict": "current" | "dated" | "unclear",
 "confidence": "high" | "medium" | "low",
 "note": "one specific sentence about what a visitor would notice first"}

The note should describe something concrete and visible - a cramped header, \
stock photography, text over a busy background, a layout that clearly assumes a \
desktop. Avoid generalities like "looks unprofessional"."""


def looks_dated(
    screenshot: bytes,
    api_key: str,
    model: str,
    timeout: int = 60,
) -> tuple[str, str]:
    """Return `(verdict, note)`. `("unclear", "")` when it could not run.

    Never raises: this is one signal among several, and losing it must not cost
    the assessment.
    """
    # A blank model is the documented "off" switch, not a misconfiguration.
    # Returning early keeps a disabled pass from making a doomed call and
    # logging a warning on every ambiguous prospect.
    if not (screenshot and api_key and model.strip()):
        return "unclear", ""

    try:
        from groq import Groq

        client = Groq(api_key=api_key, timeout=timeout)
        encoded = base64.b64encode(screenshot).decode()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _SYSTEM},
                        {
                            "type": "image_url",
                            # PSI hands back JPEG; declaring it as such avoids a
                            # decode mismatch on the provider side.
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                        },
                    ],
                }
            ],
            temperature=0.2,
            max_tokens=300,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        log.warning("vision pass failed: %s", e)
        return "unclear", ""

    parsed = _extract(raw)
    if not isinstance(parsed, dict):
        return "unclear", ""
    verdict = str(parsed.get("verdict") or "unclear").lower()
    if verdict not in ("current", "dated", "unclear"):
        verdict = "unclear"
    return verdict, str(parsed.get("note") or "")[:400]


def _extract(text: str):
    from wizcore.llm.client import extract_json

    try:
        return extract_json(text)
    except Exception:
        try:
            return json.loads(text)
        except Exception:
            return None
