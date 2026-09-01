# Repository guidance

- Keep this repository a source mirror, not a copy of a live DSH home or user
  data directory.
- Never commit model files, `node_modules`, Session history, memory databases,
  private images/assets, logs, credentials, tokens, cookies, or `.env` files.
- Do not change production runtime state from repository maintenance work.
- Make focused changes and run only the targeted checks relevant to the change.
- Claim behavior as verified only when direct test or runtime evidence supports
  it; otherwise label it unverified.
