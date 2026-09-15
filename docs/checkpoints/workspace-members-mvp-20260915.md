# Hermes Workspace Members MVP checkpoint

Goal: one owner + max one invited member inside one Hermes profile/runtime.
Confirmed design: invited member has full workspace memory access; Hermes must identify current actor and workspace owner; member tool use requires owner grant; grants are revocable and expire no later than 30 days; additional members are future paid capacity.
Implementation target: Forge Mini App invite/member/grant controls + runtime workspace-members plugin + Telegram allowlist sync. No extra Hermes daemon per member.
Safety: secrets never exposed; owner remains authority; destructive-action safety remains in force after member grant.
