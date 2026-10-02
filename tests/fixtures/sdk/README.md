# SDK response fixtures

Raw HTTP response bodies of the Anthropic Messages API. Tests serve them through
an in-process mock transport, so the real `AnthropicProvider` and the SDK's own
parsing run without a network or an API key.

Recorded live on 2026-10-02 from `claude-haiku-4-5-20251001` (the small tier):

- `text_reply.json`: a plain one-sentence answer.
- `triage_valid.json`: the example's structured triage call.
- `max_tokens.json`: a reply cut off by a small `max_tokens`.
- `error_invalid_request.json`: the API's 400 for a request with no messages.

Hand-written from the documented response shapes, because a live call cannot
produce them on demand:

- `text_reply_with_thinking.json`: a reply with a `thinking` block and cache usage.
- `triage_invalid.json`: structured output that fails the schema.
- `refusal.json`: `stop_reason` "refusal" with `stop_details`.
- `error_rate_limit.json`, `error_overloaded.json`: HTTP 429 and 529 bodies.

All ticket text is synthetic.
