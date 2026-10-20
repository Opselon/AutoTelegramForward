# Mobile UI/UX pass: what changed and what it measures

The app was usable on one phone size in one language at the default text scale.
This pass makes the shell adaptive, the palette a contrast contract, and the
lists virtualized — each pinned by a test that fails when the property is lost.

## Adaptive shell

Three window classes, declared once in `lib/core/layout.dart`:

| class | width | navigation |
|---|---|---|
| compact | `< 600 dp` | five-slot `NavigationBar` |
| medium | `600–839 dp` | labelled `NavigationRail` |
| expanded | `>= 840 dp` | rail + list/detail two-pane (`MasterDetailPane`) |

A screen that needs the two-pane split no longer hardcodes a width; it asks for
the size class.

## Colour as a measured contract

The old palette shipped combinations that fail WCAG: white on emerald measured
**2.54:1**, white on amber **2.15:1**, indigo-500 as text on the dark surface
**4.0:1** — all under the 4.5:1 body floor. The role colours were re-picked and
`test/core/palette_contrast_test.dart` now measures every role pair in both
themes, so "let's make the greens brighter" fails the build instead of shipping.

`AppPalette.readableOn(background)` picks white or slate ink by measured
contrast, so filled surfaces stop assuming white.

## Accessibility

`test/widgets/adaptive_accessibility_test.dart` walks **every destination**:

- text scale 0.8 → 1.3 renders without overflow, phone and tablet;
- a right-to-left locale mirrors the layout (first destination on the right,
  no overflow);
- interactive targets are ≥ 48 dp and icon-only buttons carry a tooltip, which
  is also what gives them a semantics label.

## Lists

The chats page built a `Column` of every tile inside its `ListView`, so a
400-chat account mounted 400 rows per frame. It is now a `CustomScrollView`
(header sliver + `SliverList.builder`) with identity keys:

| | before | after |
|---|---|---|
| rows mounted, first frame | 400 (all) | **4** |
| rows mounted after a 6000 px fling | 400 (all) | **10** |

Enforced by `test/widgets/list_virtualization_test.dart`, which drives a real
400-chat payload on a 390×844 phone.

## Startup payload

The rule editor (1.6 k lines, the largest screen) is a `deferred` import loaded
on the way in, so it is no longer part of the startup kernel load.

## States

Every data screen has the four states from `docs/ui-ux-system.md` §3: skeleton
on first load, empty with a call to action, error with retry, then content —
with the failure announced through a live region rather than a raw exception.

## Gates

- `flutter analyze` — no issues.
- `dart format lib test` — 0 changed.
- `flutter test` — **231 passed** (was 210): 19 new tests for the shell, the
  palette contrast, RTL/text-scale/a11y and list virtualization.
- `python3 scripts/audit_mobile_ui_copy.py` PASS (0 offenders, 0 baselined);
  `audit_cross_language.py --report` PASS (1:1, no cross-leak, placeholders
  match, 10 locales).
