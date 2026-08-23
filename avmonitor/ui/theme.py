"""Dark theme constants for the AV Monitor UI.

Palette keyed off the actual brand mark (brndz_icon.svg): background
`#15130f`, brand wine-red `#921212`. Warm/neutral throughout -- no blue in
any background or border tone -- with a brand-red ACCENT for active/
highlighted UI state (button borders, hovered splitters) instead of a
generic color.
"""

BG = (21, 19, 15)          # #15130f, straight from the logo background
PANEL_BG = (33, 29, 24)
PANEL_BORDER = (69, 60, 48)
TEXT = (242, 236, 226)
TEXT_DIM = (168, 154, 134)
TEXT_LABEL = (125, 112, 98)

ACCENT = (196, 55, 48)     # brand wine-red, brightened for on-screen contrast
PANEL_TITLE = (168, 45, 42)  # deeper wine, dedicated to panel headers (SISTEMA/ÁUDIO I/O/REDE/PROCESSOS)
GOLD = (232, 182, 78)      # same family as BAR_MID below, tuned for text legibility over PANEL_BG

# REC button badge: recolored from a reference "stock" bright-red REC
# pill (white text) toward this palette -- filled wine instead of vivid
# red, label text warmed toward gray/brown instead of white.
REC_IDLE_BG = (54, 30, 27)     # dark muted wine-brown, badge at rest
REC_ACTIVE_BG = (150, 40, 36)  # deeper wine than ACCENT, badge while recording

# Status colors stay semantic (green=ok/amber=warn/red=crit is a universal
# meter convention a technician reads at a glance) but warmed where it
# doesn't cost that legibility -- WARN nudged from yellow toward amber.
OK = (80, 210, 120)
WARN = (235, 160, 50)
CRIT = (235, 65, 55)

# Spectrum bar color-by-amplitude ladder: dark wine (quiet) -> gold -> orange
# -> bright red (clipping). Keeps the same "low energy is dark/muted, high
# energy is bright/saturated" reading a meter needs, just entirely in the
# warm family instead of the old cold-blue-to-red gradient.
BAR_LOW = (122, 47, 40)
BAR_MID = (232, 172, 60)
BAR_HIGH = (240, 130, 42)
BAR_CLIP = (240, 60, 50)

FONT_NAME = "consolas"

# Colorkey used for the compact/overlay window: any pixel drawn in exactly
# this color becomes transparent (see win_native.enable_colorkey_transparency).
# Pure cyan -- far from every color the warm bar palette above produces
# (magenta was too close to BAR_CLIP's red once the palette warmed up).
COMPACT_COLORKEY = (0, 255, 255)
