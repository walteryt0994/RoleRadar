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

## Icons added for the bilingual UI (2026-11-17)

Eleven more line icons for the page frame (side bar, menu) and the workspace. No network was used: their drawing
elements were read, as text and without running anything, from the icon nodes of the **lucide-react 0.542.0** npm
package already on this computer (`/Users/hug0_/hugo-site/node_modules/lucide-react/dist/esm/icons/<name>.js`, a copy
installed for another local project; its `LICENSE` is the same ISC licence with the Feather MIT notice), and written
in Lucide's own SVG file layout. The same rebuild reproduces five of the 1.64.0 files above byte for byte (`radar`,
`plus`, `arrow-left`, `chevron-right`, `lock-keyhole`), so the layout is Lucide's; the drawings themselves are those
of release 0.542.0, which may differ from a later release. These files are **not** the official repository's files
of tag 1.64.0, so the SHA-1 column below is the Git blob hash of the file as written here, recorded so a test notices
any later change; it is not a hash checked against the official repository.

| File | Source (lucide-react 0.542.0 icon node) | Git blob SHA-1 of the file here | Used for |
| --- | --- | --- | --- |
| `icons/house.svg` | `dist/esm/icons/house.js` | `8bf34bdc8dc48c3f9b557b3caeb3edb064bffba6` | Workspace (side bar) |
| `icons/user.svg` | `dist/esm/icons/user.js` | `8e61592bbf912f26d2e6583dffd9b1e8ebec0193` | My profile, the user menu |
| `icons/compass.svg` | `dist/esm/icons/compass.js` | `25bf214f546dc7fd3464f789f5a6adc1a5bca457` | Career exploration |
| `icons/briefcase.svg` | `dist/esm/icons/briefcase.js` | `ef4dbd857c3677d518112b9200537b02906d16f7` | Jobs |
| `icons/database.svg` | `dist/esm/icons/database.js` | `f95db0e3f40633742b292b2f64f5d9c37e3eb157` | Data management |
| `icons/search.svg` | `dist/esm/icons/search.js` | `03e442a5440cff92c219fe3a3c1f2b0788d30701` | Find opportunities (workspace) |
| `icons/file-text.svg` | `dist/esm/icons/file-text.js` | `5227939068ebaf76768cf348c04dd6c3db281a7d` | Add an experience (workspace) |
| `icons/file-plus.svg` | `dist/esm/icons/file-plus.js` | `e1e83ea01af1842dc014a60f0580cc5ba0df1cd6` | Add a job (workspace) |
| `icons/menu.svg` | `dist/esm/icons/menu.js` | `df35c0900894157c0eb9ee1ee07d88aa46c61204` | The menu on narrow screens |
| `icons/chevron-down.svg` | `dist/esm/icons/chevron-down.js` | `e70d30b88399a721cd5d20d0d26756608cf8c42b` | Arrow of a menu that opens downwards |
| `icons/triangle-alert.svg` | `dist/esm/icons/triangle-alert.js` | `3232fb062dc26463279016cd7483677de9ef9bbd` | Why an exploration or a search cannot start yet (its review page) |

`compass`, `database`, `search`, `chevron-down` are among the icons derived from Feather, so the MIT notice in `LICENSE` also applies to them.

## Icons added for the owner redesign (2026-11-17)

11 more line icons for the redesigned pages (and `download`, added in the owner review on 2026-11-18), rebuilt the same way as the bilingual UI's above (offline, from the **lucide-react 0.542.0** icon nodes already on this computer, in Lucide's own file layout; the rebuild reproduces the files above byte for byte). As above, the SHA-1 column is the Git blob hash of the file as written here, recorded so a test notices any later change; it is not a hash checked against the official repository.

| File | Source (lucide-react 0.542.0 icon node) | Git blob SHA-1 of the file here | Used for |
| --- | --- | --- | --- |
| `icons/calendar.svg` | `dist/esm/icons/calendar.js` | `738ea05b4876b8e5e06b8a8c05042a7a0dbbbc62` | The earliest start (the preferences) |
| `icons/check.svg` | `dist/esm/icons/check.js` | `5abe1527434e7b251bde0661f99af96178976d8c` | A chosen value (the search setup's chips) |
| `icons/circle-check.svg` | `dist/esm/icons/circle-check.js` | `2690cda6d9140a93646bedd73070c6b1b8b1c058` | Confirm this is true (a record) |
| `icons/circle-x.svg` | `dist/esm/icons/circle-x.js` | `e210b2d10168c3b8b8ab6ac33cdb75e3cf88322d` | 跳过 / Skip (a job's decision) |
| `icons/clock.svg` | `dist/esm/icons/clock.js` | `7acbafe109e4eceee0dc25862e643319c548cd81` | How long an import is kept (resume import) |
| `icons/ellipsis.svg` | `dist/esm/icons/ellipsis.js` | `68456c39be516d1d14aee329ca3b0056ba84d3c5` | The More menu of a page or a record |
| `icons/file-up.svg` | `dist/esm/icons/file-up.js` | `449407976f8b31271219c7d04a3c7baf678c8476` | Drop a resume here (resume import) |
| `icons/pencil.svg` | `dist/esm/icons/pencil.js` | `7384498e8679ff3c60effccb5723ed1672544e27` | Edit (a record, the preferences) |
| `icons/star.svg` | `dist/esm/icons/star.js` | `b35bedd29fe765b729b3cd790e98574c4afde4c0` | 收藏 / Favorite (a job's decision) |
| `icons/upload.svg` | `dist/esm/icons/upload.js` | `ce355073f92b8947dd9e439c6ff67e6a13b837ae` | Import a resume (library, workspace) |
| `icons/x.svg` | `dist/esm/icons/x.js` | `e31794349d3eecea1b3ae8e974b68bb8f4becda7` | Remove a row (the preferences editor) |
| `icons/download.svg` | `dist/esm/icons/download.js` | `5c75527bde1fe8259efb72c5b590bedfae28c05e` | Download the original file or the text read (resume import; added in the owner review, 2026-11-18) |

`calendar`, `check`, `clock`, `upload`, `x` are among the icons derived from Feather, so the MIT notice in `LICENSE` also applies to them. `download` (added in the owner review) is derived from Feather too, so the same notice applies to it.

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
