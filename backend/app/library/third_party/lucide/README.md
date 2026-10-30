# Lucide icons used by the experience library

Six line icons from [Lucide](https://lucide.dev), copied unchanged from the
official repository at release **1.64.0** (tag `1.64.0`, commit
`be09619c1c1124ad09c19fc3e87ee3b939409bbd`), together with its licence:

| File | Official path | Git blob SHA-1 at the tag | Used for |
| --- | --- | --- | --- |
| `icons/radar.svg` | `icons/radar.svg` | `bb58d7751c597cfaa96e1bd77a597a0a50ccdefa` | RoleRadar brand mark (white on a dark grey tile) |
| `icons/plus.svg` | `icons/plus.svg` | `41f92bb93eb6626368d71c9884127bbcf7d3ccdf` | New record |
| `icons/arrow-left.svg` | `icons/arrow-left.svg` | `b40bff851c85eff44f678eb7984fc6070d7ea721` | Back links: to the library (narrow screens) and to the history |
| `icons/rotate-ccw-clock.svg` | `icons/rotate-ccw-clock.svg` | `67152e4fbda9c61602d11985f3f1fa74136f00d7` | View history (Lucide's former `history` icon) |
| `icons/lock-keyhole.svg` | `icons/lock-keyhole.svg` | `97fcbd189d5f5d855d26bb1698680367cf3d9391` | Private note (one per page) |
| `icons/chevron-right.svg` | `icons/chevron-right.svg` | `db62a0c20181edd6173722b2a483e0b85e30f240` | Arrow of a folding section (turns down when open) |
| `LICENSE` | `LICENSE` | `718bb3f0e44153809972abed31839375804bf652` | Licence text |

Sources: <https://github.com/lucide-icons/lucide/tree/1.64.0/icons>,
<https://lucide.dev/license> and
<https://github.com/lucide-icons/lucide/blob/1.64.0/LICENSE>.

Licence: every Lucide icon is under the ISC licence in `LICENSE`. `plus`,
`arrow-left` and `chevron-right` are among the icons derived from Feather, so
the MIT notice in the same file also applies to them. These are general open-source icons, not
artwork made for RoleRadar.

How they are used: `templates/_icons.html` repeats the drawing elements of
these files unchanged inside a fixed `icon(name)` macro, as inline SVG. The
macro only sets the outer `<svg>` attributes: an 18px size (21px inside the
brand mark and 16px for the fold arrow, set by CSS), `stroke-width="1.7"` instead of Lucide's default 2
for a lighter line at that size, and `aria-hidden="true"` /
`focusable="false"`, because each icon sits next to the text that names its
control. No icon package, script, font or remote request is used, and no
user input is ever placed inside an icon.
`tests/library/test_library_ui.py` checks that the macro still matches these
files and that the files still have the blob SHAs above.

To update an icon, replace its file with the official one from a newer tag,
record the new tag and SHA here, and copy its drawing elements into the macro.
