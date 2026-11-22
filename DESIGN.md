---
name: Copy Monitor Website
description: A calm relay control surface for understanding a local Windows monitoring tool.
colors:
  paper: "#f4f6f7"
  white: "#fff"
  navy: "#172e43"
  ink: "#192f42"
  muted: "#566777"
  line: "#d5dce1"
  mist: "#e5ecef"
  panel-muted: "#b4c7d4"
  panel-line: "#425a6d"
  lime: "#bce963"
  lime-hover: "#cefa7b"
  green: "#365c27"
  blue: "#2c5d80"
  navy-hover: "#2b4a62"
  panel-active: "#29465c"
  panel-marker: "#8da6b8"
typography:
  display:
    fontFamily: "Noto Sans SC, Microsoft YaHei, sans-serif"
    fontSize: "clamp(52px, 5.1vw, 76px)"
    fontWeight: 800
    lineHeight: 1.25
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "Noto Sans SC, Microsoft YaHei, sans-serif"
    fontSize: "clamp(30px, 3.1vw, 44px)"
    fontWeight: 700
    lineHeight: 1.5
    letterSpacing: "-0.025em"
  title:
    fontFamily: "Noto Sans SC, Microsoft YaHei, sans-serif"
    fontSize: "19px"
    fontWeight: 650
    lineHeight: 1.65
    letterSpacing: "-0.015em"
  body:
    fontFamily: "Noto Sans SC, Microsoft YaHei, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.85
  explanation:
    fontFamily: "Noto Sans SC, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.9
  state-message:
    fontFamily: "Noto Sans SC, Microsoft YaHei, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.9
  action:
    fontFamily: "Noto Sans SC, Microsoft YaHei, sans-serif"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1.5
  metadata:
    fontFamily: "Noto Sans SC, Microsoft YaHei, sans-serif"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.85
  measure:
    fontFamily: "Barlow, Noto Sans SC, Microsoft YaHei, sans-serif"
    fontWeight: 600
rounded:
  compact: "6px"
  control: "8px"
  overlay: "12px"
  surface: "16px"
  round: "50%"
spacing:
  tight: "8px"
  small: "12px"
  compact: "16px"
  medium: "20px"
  roomy: "24px"
  inset: "28px"
  large: "32px"
  block: "40px"
  section-heading: "48px"
  section: "112px"
components:
  button-primary:
    backgroundColor: "{colors.navy}"
    textColor: "{colors.white}"
    typography: "{typography.action}"
    rounded: "{rounded.control}"
    padding: "13px 20px"
    height: "50px"
  button-primary-hover:
    backgroundColor: "{colors.navy-hover}"
  button-lime:
    backgroundColor: "{colors.lime}"
    textColor: "{colors.navy}"
    typography: "{typography.action}"
    rounded: "{rounded.control}"
    padding: "10px 14px"
    height: "42px"
  button-lime-hover:
    backgroundColor: "{colors.lime-hover}"
  relay-surface:
    backgroundColor: "{colors.navy}"
    textColor: "{colors.white}"
    rounded: "{rounded.surface}"
  feature-container:
    backgroundColor: "{colors.mist}"
    textColor: "{colors.ink}"
    rounded: "{rounded.surface}"
    padding: "36px 40px 32px"
  screenshot-dialog:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.overlay}"
    padding: "20px"
    width: "min(1188px, calc(100% - 40px))"
---

# Design System: Copy Monitor Website

## Overview

**Creative North Star: "The Relay Control Panel"**

The website gives a visible form to a handoff: waiting, applying, checking an uncertain result, and confirming completion. Fog-white reading space surrounds a navy control surface; lime identifies an available action or an explicit signal. The atmosphere is precise, calm, and practical, with planar divisions and geometric SVG marks.

This system applies to the static website in `site/` only. It does not prescribe the Python/Tk desktop application's appearance. Chinese reading comes first; the product identity and large step numbers provide a restrained Latin counterpoint. The site's product explanation and simulated states remain distinguishable from the real desktop screenshot.

**Key Characteristics:**

- Fog-white reading surfaces, navy control surfaces, and lime signals.
- Large Chinese headings with readable explanations and quiet metadata.
- Flat sections joined by one-pixel divisions and generous space.
- Manual, finite demonstrations with named states and explicit confirmation.
- Existing software imagery and factual copy, with no fabricated social proof.

## Colors

The palette pairs cool neutral reading space with a dark control surface and a single luminous action accent. The frontmatter owns the exact values; the following names describe their application.

### Primary

- **Relay Navy** (`navy`): the interactive panel, primary download buttons, and geometric marks; **Navy Hover** changes primary action state.
- **Signal Lime** (`lime`): the demonstration action, selected/current/complete signals, text selection, and the download section. **Lime Hover** is its button hover state.

### Neutral

- **Fog Paper** (`paper`) is the page background; **White** supplies high-contrast text inside navy surfaces.
- **Reading Ink** (`ink`) is the default foreground; **Slate Copy** (`muted`) supports explanatory text on light surfaces.
- **Mist** (`mist`) creates broad tonal separation around the real interface and the feature container.
- **Light Divider** (`line`) separates content on paper; **Panel Divider** (`panel-line`) separates content inside the navy panel.
- **Panel Copy** (`panel-muted`) supports readable secondary text inside navy; **Panel Marker** is the inactive radio/path stroke; **Panel Active** backs selected mobile modes and reset hover.
- **Grounded Green** (`green`) marks the title stop, small light-surface status dot, and confirmation icon. **Focus Blue** (`blue`) supplies light-surface focus outlines and link hover.

**The Signal Has Meaning Rule.** Use lime for actions, selected/current/complete states, and the existing broad download surface. Keep the state name in text alongside its signal.

## Typography

**Display and Body Font:** self-hosted Noto Sans SC, falling back to Microsoft YaHei and sans-serif. The supplied variable face covers weights (400–900).

**Identity and Measure Font:** self-hosted Barlow at weight (600), falling back to the Chinese stack. It is used for the Latin wordmark and large setup numerals; log timing also uses tabular numeric alignment.

The type ramp is deliberately steep between headings and reading copy. It is not a modular ratio. Headings balance their lines, paragraphs use pretty wrapping, and explanatory copy is limited to (72ch) where the layout permits.

- **Display:** the frontmatter's fluid heading; at widths up to (820px) it becomes (54px), and up to (600px) it becomes `clamp(46px, 12.5vw, 64px)`.
- **Headline:** the frontmatter's fluid section heading; (32px) up to (820px), then (30px) up to (600px). The download heading has its own larger observed range, `clamp(36px, 3.5vw, 48px)`, and becomes (36px) up to (820px).
- **Title:** the frontmatter's base title. Dense feature/interface titles adjust within (14–19px); the large statement title uses (28px), (23px) up to (820px), and (26px) up to (600px).
- **Body and explanation:** body is the default document text. Product explanations use the `explanation` role at every viewport; state messages use `state-message` above (600px) and the explanation size below it.
- **Actions and mode labels:** actions use the `action` role. Main mobile download actions, the guide link, FAQ questions, and download resource rows rise to (14px). Mode labels are (14px) on wide layouts and (12px) at tablet/mobile widths; mobile mode labels have line height (1.6).
- **Metadata:** simulation notices, timing, path captions, figure captions, and minor provenance use the `metadata` role. The brand sublabel and screenshot action remain at least (11px); version information and the demonstration explanation use (12px). Funds/start reminders and footer risk copy use (12px) with line height (1.9).

**The Readable Explanation Rule.** Apply the final responsive text rules: product explanations stay at (14px); state messages stay at (13px) on desktop and (14px) on mobile; action text stays at least (13px), mode labels at least (12px), and metadata at (11px). The earlier smaller declarations in the stylesheet are superseded source history, not reusable type tokens.

## Layout

The desktop content container is `min(1248px, calc(100% - 112px))`, centered. It changes to `calc(100% - 72px)` up to (1100px), `calc(100% - 48px)` up to (820px), and `calc(100% - 40px)` up to (600px). Section space is the frontmatter's base section token, then (80px) up to (820px), then (64px) up to (600px). Large screens from (1450px) increase the hero top inset to (76px).

The current hero places the heading and explanatory action area in two columns (1.1fr / 1fr), above a full-width relay surface. The relay's controls/display split is (300px / 1fr), then (260px / 1fr) up to (1100px), and (220px / 1fr) up to (820px). At (600px), the hero and relay stack; the three mode choices remain a three-column row above the state display.

Reading sections use paired columns, rows with dividers, or three setup steps. At (820px), the screenshot and its notes stack while the notes form three columns. At (600px), feature content, interface notes, setup steps, FAQ, and download content become single-column; setup numbers occupy a narrow left rail. The path itself remains three columns. Preserve its short labels and allow event text and playback controls to wrap.

The mobile menu replaces inline navigation at (600px). The intermediate navigation hides the software-interface link from (601–820px), and the expanded mobile menu exposes all links. The screenshot dialog uses a scrolling image viewport; on mobile, the image has a (700px) minimum width so the real software can be inspected rather than shrunk beyond recognition.

## Elevation & Depth

Most content is flat. Mist backgrounds, one-pixel dividers, and navy/white reversal establish hierarchy. Soft shadows are limited to primary-action hover, the real software screenshot, the mobile navigation overlay, and the image dialog; exact shadow values are in the sidecar. There are no offset shadows or decorative gradients in the built system.

**The Depth Follows Function Rule.** Keep ordinary reading sections flat. Use the observed soft elevation for actionable imagery and overlays, and preserve the darker modal backdrop so the screenshot becomes the focus.

Motion explains a state change. Hover treatments use (0.2s); path-node background uses (0.4s) with `cubic-bezier(.16,1,.3,1)`, and path-label color uses (0.3s). The demonstration starts only on a user's play action and ends after four events, with follow-up events at (2200ms), (4700ms), and (7600ms). Reduced motion disables transitions and smooth scrolling and displays the selected mode's final event immediately.

## Shapes

The recurring silhouette is a gently rounded rectangle: surface corners use the `surface` radius, controls use `control`, and overlays use `overlay`. Compact mobile mode choices and dialog-close controls use `compact`. The feature tag is an observed small rectangle with corners (4px), not a new global radius scale. Signals use circles for radio/path progression and small squares for the relay title and interface annotations.

Borders are thin (1px) and tonal. Geometric line icons use rounded caps/joins and the current foreground; standard icons are (20px) with stroke width (1.7), while dense controls use (15–17px) variants. Use actual SVG paths, not text glyph substitutes.

## Components

### Buttons and text links

Primary download actions are navy with white text; the final download section reuses the same treatment. Buttons have a minimum height, not a fixed text box: the frontmatter's `height` records that minimum. Primary buttons use the recorded padding and radius, a (12px) content gap, and a soft shadow on hover. The relay's lime play action uses a (42px) minimum height and a smaller (8px) gap. Its disabled playing state uses the observed fill (`#93b651`) and navy text.

The relay reset is a bordered icon button (40px square, 42px on mobile). Text links keep an underline with offset (5px), and hover shifts toward Focus Blue. Global keyboard focus is a (3px) outline with (5px) offset; within the navy relay, the outline changes to lime.

### Navigation and menu

Desktop navigation is a horizontal row with (13px), weight (500) text, (32px) gaps, and simple underlined hover. Tablet gaps reduce before the mobile breakpoint. The mobile trigger is (42px) square. Its opened menu sits below the header on Fog Paper with a thin border, overlay corners, row dividers, and soft elevation. The trigger exposes `aria-expanded` and `aria-controls`; choosing a link or clicking outside closes it. Escape closes it and restores focus to the trigger.

### Relay surface, modes, and path

The navy surface has rounded outer corners, clipped contents, a title/status band, and internal panel dividers. Mode selection is a real labelled radio group. Desktop choices are separated rows with a circular marker, title, secondary explanation, and a selected arrow. Selection uses a lime marker and white text. Mobile choices become equal-width bordered boxes with a selected tonal fill; the secondary line and arrow are hidden. Radio keyboard focus outlines the entire choice with a (2px) lime outline and (4px) offset.

The ordered three-step path combines circular nodes and connecting lines. A current node and label use lime; completed nodes fill lime with navy centers/checkmarks, and completed connectors turn lime. Preserve text labels and captions so color is not the sole indicator. The event log is a polite, atomic live status region, separated by top/bottom rules. Playback disables its own button, reset clears scheduled events, mode changes reset the sequence, and hiding the page resets a playing demonstration. It never represents a live account.

### Tonal containers and tags

The main feature container uses Mist, surface corners, and the recorded inset. It uses tonal grouping rather than a shadow. Small capability tags are quiet outlined annotations, not action chips; their metadata type and compact border keep them subordinate to the explanation.

### Disclosures

FAQ rows use native `details`/`summary` controls with one-pixel horizontal separators. Each summary distributes the question and a plus SVG across the row. Opening rotates the plus (45deg); the answer remains a normal paragraph with comfortable leading. The source begins with the first answer open and permits multiple answers to remain open. Hover uses Focus Blue and keyboard focus retains the global outline.

### Screenshot and focus dialog

The real desktop screenshot is an image button with a soft shadow and a bottom-right navy expand label. Keep the existing image's proportions and honest caption. Activation calls the native modal dialog, with an image-window fallback where `showModal` is unavailable. The modal has a paper surface, overlay corners, a navy-tinted backdrop, a clear close button, and an internally scrolling image region; the page behind it stops scrolling. Native modal focus handling, Escape dismissal, and the explicit close control remain available. A backdrop click closes only when the click falls outside the dialog bounds.

## Do's and Don'ts

### Do:

- **Do** apply this system to website surfaces only and retain the desktop screenshot as product evidence.
- **Do** keep explanatory copy at the final readable sizes and use the established font pairing.
- **Do** pair status color with a named state, and keep the demonstration visibly labelled as simulated.
- **Do** preserve native radios, disclosures, dialog semantics, keyboard focus, and reduced-motion behavior.
- **Do** use tonal separation and one-pixel rules before introducing elevation.

### Don't:

- **Don't** reuse superseded small text declarations or reduce functional labels below (11px).
- **Don't** turn an unconfirmed result into a success signal or remove its accompanying explanation.
- **Don't** add fabricated pricing, customer quotations, returns, or performance proof to support the visual identity.
- **Don't** replace the product's existing geometric SVG icon language with text glyphs.
