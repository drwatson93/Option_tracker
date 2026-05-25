import base64
import json
import re
from pathlib import Path
from typing import Optional
import anthropic

SYSTEM_PROMPT = """You are an assistant that extracts options trade data from screenshots of brokerage platforms (Robinhood, Schwab, TD Ameritrade, tastytrade, etc.).

Extract the following fields if visible:
- symbol: ticker symbol (e.g. "AAPL")
- option_type: one of "CC" (covered call), "CSP" (cash-secured put), "Call", "Put"
- strike: numeric strike price (e.g. 200.0)
- expiration_date: ISO format YYYY-MM-DD
- quantity: number of contracts (positive integer)
- open_premium: credit received per share if selling, or debit paid per share if buying (positive number)
- open_date: ISO format YYYY-MM-DD if visible, otherwise null
- notes: any other relevant context visible in the screenshot

Rules:
- If you see "Sell to Open" or "STO" for a call → option_type = "CC"
- If you see "Sell to Open" or "STO" for a put → option_type = "CSP"
- If you see "Buy to Open" or "BTO" for a call → option_type = "Call"
- If you see "Buy to Open" or "BTO" for a put → option_type = "Put"
- open_premium should be per share (divide total credit/debit by 100 * contracts if needed)

Respond ONLY with a valid JSON object, no markdown, no explanation.
Use null for any field you cannot determine.

Example:
{"symbol": "AAPL", "option_type": "CSP", "strike": 180.0, "expiration_date": "2025-02-21", "quantity": 1, "open_premium": 2.35, "open_date": null, "notes": null}"""


ALLOWED_TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
}


class AIParseError(Exception):
    pass


def allowed_image(filename: str) -> tuple[bool, str]:
    ext = Path(filename).suffix.lower()
    media_type = ALLOWED_TYPES.get(ext)
    return (media_type is not None, media_type or '')


def parse_screenshot(image_bytes: bytes, media_type: str, api_key: str) -> dict:
    """
    Sends image to Claude Vision and returns extracted trade fields.
    Raises AIParseError on failure.
    """
    b64 = base64.standard_b64encode(image_bytes).decode('utf-8')

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': media_type,
                            'data': b64,
                        },
                    },
                    {
                        'type': 'text',
                        'text': 'Extract the options trade data from this screenshot.',
                    },
                ],
            }
        ],
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences if the model added them
    raw = re.sub(r'^```json\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AIParseError(f'Could not parse AI response as JSON: {e}\nRaw: {raw}')

    return data
