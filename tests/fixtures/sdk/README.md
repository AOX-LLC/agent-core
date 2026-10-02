# SDK response fixtures

Raw HTTP response bodies of the Anthropic Messages API, used to drive the real
`AnthropicProvider` through an in-process mock transport, so tests exercise the
SDK's own parsing without a network or an API key.

They are hand-written from the documented API shapes, not recorded live. Success
bodies follow the Messages API response format (curl examples, including `usage`
cache fields, `thinking` blocks and `stop_details` on refusals); error bodies
follow the documented error format (`error-codes.md`) for HTTP 429, 400 and 529.
All ids, tickets and text are synthetic.
