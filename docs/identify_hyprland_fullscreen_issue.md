# Identify window loses Omarchy tiled-fullscreen state

Status: **root cause found and fixed in the app** (2026-10-29). The
Hyprland binding workaround described below was removed the same day and the
stock Omarchy `Ctrl+Super+F` binding restored.

## Root cause

`IdentifyInfoTab._clear()` removed its row labels with `widget.setParent(None)`
before `deleteLater()`. `_render()` creates each label parentless and adds it to
the layout, which queues a deferred "show if not hidden" for the label. The Info
tab re-renders several times per navigation (observation, pending actions,
login), so labels added by one render were detached by the next before their
queued show ran. The queued show then mapped each detached label as its own
untitled top-level window (title = application name, "Observation
Workbench"). Hyprland took the fullscreen Identify window out of fullscreen to
map each one, and they vanished again when `deleteLater` ran, which also
showed up as a flash/jump.

Evidence: a Hyprland `socket2` trace showed ~14 `openwindow` events per
keypress for windows titled "Observation Workbench", each preceded by
`fullscreen>>0`; an app-wide Qt event filter showed the windows were the Info
tab's `QLabel`s (`<h3>Observation details</h3>`, `<b>Observer</b>…`).

Fix: `_clear()` now calls `widget.hide()` instead of `setParent(None)`;
hiding blocks the queued show. Any fullscreen mode (`Super+F`,
`Super+Alt+F`, `Ctrl+Super+F`) was affected, which is why the
compositor-owned binding below did not help.

## Symptom

In the Identify window, `Ctrl+Super+F` initially makes the window appear
fullscreen. Pressing an Identify shortcut such as `A` (Agree) or `R`
(Reviewed) can then make the window return to its tiled size.

## Environment observed

- Hyprland 0.56.2
- Omarchy's stock `Ctrl+Super+F` binding, labelled **Tiled full screen**
- Native Wayland PySide6/Qt application (`class: python3`, title:
  `iNaturalist Identify`)
- `hyprctl configerrors` reported no configuration errors

## Current diagnosis

The Identify shortcuts do not resize the window. `A` and `R` only start their
respective application actions, and the Identify window contains no
`showNormal()`, `showFullScreen()`, or equivalent action-specific window-state
change.

The stock Omarchy binding runs
`omarchy-hyprland-window-tiled-fullscreen-toggle`. At the time this was
investigated, that command toggled Hyprland's fullscreen state between:

```text
internal=0, client=2
internal=0, client=0
```

Hyprland documents `internal=0, client=2` as leaving the window in the normal
compositor layout while reporting fullscreen state to the application. This
client-only state is intentionally different from compositor-owned fullscreen.
The leading hypothesis is that Qt later requests or acknowledges a normal
client state during an interaction, which clears the client-only fullscreen
state and exposes the original tiled geometry. This is consistent with the
symptom, but an event-level trace has not yet been captured.

Reference: [Hyprland fullscreen-state documentation](https://wiki.hypr.land/Configuring/Basics/Dispatchers/#fullscreenstate)

## Current workarounds

- `Super+F`: regular fullscreen
- `Super+Alt+F`: maximized/full-width mode

These use compositor-managed geometry and should be less susceptible to a Qt
client-state change.

## App-specific workaround (removed)

The user binding for `Ctrl+Super+F` now calls:

```text
~/.local/bin/observation-workbench-fullscreen-toggle
```

For a window whose exact title is `iNaturalist Identify`, the helper toggles
Hyprland between `internal=2, client=0` and `internal=0, client=0`. Hyprland
therefore owns the fullscreen geometry and Qt remains in its normal client
state. For every other window, the helper delegates unchanged to Omarchy's
stock `omarchy-hyprland-window-tiled-fullscreen-toggle` command.

The override is in `~/.config/hypr/bindings.lua`. The previous binding file
was backed up as:

```text
~/.config/hypr/bindings.lua.bak.20261029-004245
```

After installation, `hyprctl reload` succeeded, `hyprctl configerrors`
reported no errors, and Hyprland reported exactly one live
`Ctrl+Super+F` binding.

## Options to revisit

1. If the app-specific workaround proves reliable, keep it and recheck its
   dispatcher syntax after major Hyprland upgrades.
2. Add an application-owned fullscreen shortcut to Identify if consistent
   cross-desktop behavior is preferable to a Hyprland-specific override.
3. If the issue persists, record `fullscreen` and `fullscreenClient`
   from `hyprctl activewindow -j` immediately before `Ctrl+Super+F`, after it,
   and after pressing `A` or `R`.

If a Hyprland override is chosen later, put it in
`~/.config/hypr/bindings.lua`; do not edit the packaged files under
`/usr/share/omarchy/`.
