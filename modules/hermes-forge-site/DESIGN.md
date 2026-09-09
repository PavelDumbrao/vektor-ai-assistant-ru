# Hermes Forge Web Design System

## Product idea
Hermes Forge is an AI employee foundry. The visual metaphor is a precise industrial forge: heat, metal, sparks, controlled machinery, not fantasy fire.

## Non-negotiable direction
- Dark graphite base, warm molten orange as the only dominant accent.
- Strong editorial typography and asymmetrical composition.
- High information density only where it helps comprehension.
- Large negative space around primary ideas.
- Product UI should feel engineered, not decorative.
- The memorable visual is the Forge Core: an orange energy ring assembling an AI employee.

## Ban the defaults
- No purple or blue SaaS gradients.
- No generic glassmorphism everywhere.
- No three-identical-cards section as the main visual pattern.
- No random floating pills, fake charts, stock people or stock robots.
- No over-rounded 24px cards. Radius stays restrained.
- No vague headlines like "Transform your workflow".
- No lorem ipsum or placeholder product copy.

## Palette
- canvas: #070707
- surface: #0d0d0d
- elevated: #131313
- text: #f4f1eb
- muted: #9a958d
- line: rgba(255,255,255,.10)
- forge: #ff5b1f
- forge-hot: #ff9a3d
- success: #86f7a7

## Typography
- Display: Unbounded, 600/700, tight tracking, Cyrillic capable.
- Body: Manrope, 400/500/600.
- Use clamp() for large text and keep line length bounded.

## Spatial system
- 8px base rhythm.
- Desktop content width: 1180px max.
- Section vertical rhythm: 96-144px desktop, 72-96px mobile.
- Borders are thin and quiet. Orange is for state, direction and energy.

## Motion
- Prefer one strong hero animation plus subtle hover feedback.
- Never hide primary content waiting for JavaScript.
- Respect prefers-reduced-motion.
- Keep transforms GPU-friendly and avoid layout thrashing.

## AI editing protocol
1. Read this file before changing UI.
2. State the intended visual change in one sentence.
3. Change one section/component at a time.
4. Check desktop and mobile after each meaningful change.
5. Reject any output that reintroduces banned defaults.
6. Preserve real product language and factual capability status.
7. Run visual screenshot QA before shipping.
