# Hermes client diagnostics checkpoint — 2026-09-14

- Vyacheslav profile only: `display.busy_input_mode` changed from `queue` to `steer`; runtime readback confirms `steer`, service active, Telegram reconnected, NRestarts=0. Other profiles remain `queue`.
- Bebov Maton connections observed ACTIVE: Zoom, Zoom Admin, Fathom.
- Standard Maton Zoom action discovery exposes no recording/transcript actions; Zoom Admin action discovery reports app inaccessible through generic catalog despite active connection.
- Direct Maton Zoom gateway read works: `zoom /users/me` = HTTP 200.
- Direct recordings endpoint works for both Zoom and Zoom Admin, but returned 0 cloud recordings for 2026-09-01..2026-09-14.
- Zoom Admin settings read succeeds: cloud recording enabled, recording audio transcript enabled, automatic recording currently disabled (`none`).
- Fathom profile tools currently read recent meetings and transcript segments successfully through Maton.
- No write action was performed against Zoom, Zoom Admin, Fathom, or Notion during diagnostics.
