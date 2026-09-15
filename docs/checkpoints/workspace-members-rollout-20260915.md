# Hermes Workspace Members rollout checkpoint

Source: PR #97 merged as 6525751fdf60d012d5f42dd2c530dafd3c7c33b3.
Production Forge API/control: active; /healthz OK; Team UI and member routes present.
Manager vendor: workspace-members plugin present; provisioner includes workspace member install for future profiles.
Fleet: pavel, baysangur, vyacheslav, bebov, salavat all have workspace-members enabled and loaded; all services active; NRestarts=0.
MVP contract: owner + max 1 invited member; one runtime/profile; full shared memory; current actor + owner identity injected every member turn; member tools owner-gated; grants 1/7/30 days and revocable in Forge; existing destructive-action safety remains.
No member has been invited automatically during rollout.
