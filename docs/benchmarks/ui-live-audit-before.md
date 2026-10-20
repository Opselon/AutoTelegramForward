# Live dashboard audit — before the UI overhaul

Measured against the deployed build (`sha-a4575f8`, https://api.legoten.com) with a real
authenticated session, by rendering the app inside a viewport-sized iframe at 360 / 768 / 1440 px and
reading layout metrics from the document. Regenerate the same way after the overhaul and compare.

| Width | Page-level overflow | Elements clipped in scrollers | Tap targets < 44 px | Icon-only buttons with no name | Inputs | Unlabelled inputs |
|---|---|---|---|---|---|---|
| 360 px | **0** (no page scroll) | **64** | **16** | 1 | 3 | 0 |
| 768 px | 0 | **51** | 16 | 1 | 3 | 0 |
| 1440 px | 0 | 25 | 16 | 1 | 3 | 0 |

## What the numbers mean

1. **The page never scrolls sideways — the content scrolls inside it.** 64 elements at 360 px sit inside
   horizontal scrollers: the dashboard keeps its desktop rows (stat strips, task rows, action bars) and lets them
   clip. On a phone the user has to drag inside several strips to see one screen's content. Target: **0 clipped
   elements at 360 px**; rows become stacked cards below 768 px.
2. **The type scale goes down to 10.4 px.** Measured font sizes on the live dashboard:
   `10.4, 10.88, 11.2, 11.52, 11.84, 12, 12.48, 12.8, 13.12, 13.6, 16, 16.8, 20`. The contract sets the floor at
   12 px (`type-xs`); below that is unreadable at arm's length. Target: **no text under 12 px**.
3. **16 tap targets are smaller than 44 px** at every width, and one icon-only button has no accessible name.
   Target: **0 targets under 44 px, 0 unnamed icon buttons**.
4. **There is no mobile navigation affordance** — the sidebar is a desktop aside (`sidebarVisibleAt360: false`,
   no drawer trigger), so at 360 px the app is a desktop layout squeezed into a phone viewport.

## Why measure this way

The iframe renders the real production bundle against the real API with a real session, so the numbers reflect
what a user gets — not what a test fixture believes. It also keeps the audit independent of whatever CSS the
overhaul introduces: the same script can be re-run on the new build and diffed.
