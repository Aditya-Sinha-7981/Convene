# Convene Mascot Assets

## Files

- `convene-mascot.svg` - full core mascot
- `convene-avatar.svg` - head-only participant avatar
- `convene-walk-01.svg` ... `04.svg` - four walking frames
- `convene-walk-sprite.png` - 4-frame transparent PNG sprite sheet

## Dynamic participant colors

The avatar SVG uses `currentColor`, so the same asset can be recolored per participant.

Example:

```html
<img src="/assets/convene-avatar.svg" style="color:#6D4AFF">
```

For inline SVG, use CSS:

```css
.speaker-avatar {
  color: var(--speaker-color);
}
```

Keep one reserved Convene brand color for the product mascot itself. Participant colors should be assigned from a controlled palette or generated from a stable participant ID so the same speaker keeps the same color throughout a meeting.

## Sprite sheet

The PNG is 1280x360 and contains four 320x360 frames in order:

0 → 1 → 2 → 3 → repeat

CSS background positioning can animate it, or the individual SVG frames can be used directly for maximum scalability.
