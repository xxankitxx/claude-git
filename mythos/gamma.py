"""
MYTHOS — GammaWatch: the two-tier gamma-explosion forewarning.

The user mandate: "if a gamma explosion is about to happen, tell me IN ADVANCE"
— but never cry wolf. So two tiers, escalating:

  LOADING  — a QUIET, silent pre-alert. A coil is winding up: dealers pinned
             short gamma at a flip level, the range gone dead (market_state ==
             COILING), convexity loaded (gamma_heat high). We say "a move is
             loading" and shut up. No chime. (commentary.note → silent path.)
  IGNITING — the LOUD tier, near-certain. The coil has BROKEN out of its band
             AND one genuinely independent axis confirms (a realized-vol kick,
             a fresh dealer-gamma flip negative, or futures flow accelerating).
             Fires once, then locks out. (commentary._fire → chime path.)

Everything reuses shipped engines — signals.market_state() for COILING, the
existing per-strike gamma already computed in the GEX loop for the flip level,
commentary._last_gex for the prior-gex read. No second coil engine, no greeks
re-pass. Detection is RARE by construction: a dwell before LOADING, a break +
independent confirm before IGNITING, and a cooldown after.
"""

import time

from . import config

# module-literal timings (behavioural, never tuned — so not in config)
GAMMA_LOAD_DWELL = 10.0     # COILING+heat must persist this long before LOADING speaks
GAMMA_LOAD_DECAY = 20.0     # LOADING de-escalates silently after this long with no coil
GAMMA_COIL_WINDOW = 180.0   # realized-vol "then" lookback for the IGNITING rv-kick vote


def _pool(pool, now):
    """Rotate phrasing by the clock — the shipped _FLASH_LEAD trick. No repeats
    back-to-back without a new class."""
    return pool[int(now) % len(pool)]


def _fill(tpl, **kw):
    out = tpl
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


_BLASTER_LOADING = [   # SILENT, calm — flat OR in-trade. Never says "hunt" (ARMED owns that).
    "Something's coiling at {flip} — gamma is pinning price and the range has gone dead. "
    "A move is loading; don't pre-empt it.",
    "Energy stacking into {flip}: convexity loaded, tape quiet. This is the wind-up — "
    "let it show its hand.",
    "Coil tightening at {flip}. When this releases it'll be fast — hands ready, eyes up, "
    "no early bet.",
]
_BLASTER_IGNITING = [  # LOUD (chime). {dir} resolved at the break.
    "IGNITING — {flip} just snapped {dir} and dealers are forced to chase. This is the leg; "
    "ride the trail, not the tick.",
    "It's going. {flip} gave way {dir}, the tape just turned into an accelerant — this is the "
    "move the coil was hiding.",
    "IGNITING {dir} through {flip}. Dealers short gamma now; the next points come fast. "
    "If you're in, do NOT touch it.",
]


class GammaWatch:
    def __init__(self, flow):
        self.flow = flow
        self.stage = "idle"           # idle | loading | igniting(transient) | cooled
        self.load_start = 0.0         # when COILING+heat first held (for the dwell)
        self.load_since = 0.0         # when LOADING began (ignite must be within window)
        self.cooled_since = 0.0       # when the post-ignite lockout began
        self._decay_start = 0.0       # when the coil started dying (silent de-escalate)
        self.coil_mid = 0.0           # the level a break is measured from
        self.dir = ""                 # 'up' | 'down', resolved at the break
        self.last_ignite_text = ""    # for the in-trade radar route
        self.flow_accel = False       # set by the caller = flow.cvd.accelerating()
        self._last_loading_fire = -1e9

    # ── flip level — fed the per-strike signed gamma already built in the GEX loop
    def flip_level(self, signed_g: dict) -> float:
        """Strike where cumulative dealer gamma crosses zero (the coil/pin level).
        Rounded to the nearest real strike — no sub-strike fake precision. 0.0 if
        the chain has no in-band sign change (common; detector goes inert)."""
        if not signed_g:
            return 0.0
        ks = sorted(signed_g)
        cum = 0.0
        prev_k = None
        prev_c = None
        for k in ks:
            cum += signed_g[k]
            if prev_c is not None and (prev_c < 0) != (cum < 0):
                # crossing between prev_k and k — pick the nearer strike (no interp)
                return float(k if abs(cum) < abs(prev_c) else prev_k)
            prev_k, prev_c = k, cum
        return 0.0                    # one-sided book → no flip → inert

    # ── the state machine, once per analytics pass ───────────────────────────
    def scan(self, spot, gamma_heat, gex, prev_gex, regime_state,
             realized_now, realized_then, flip_level, now, fire_note, fire_alert,
             in_trade):
        """fire_note = commentary.note (silent, LOADING); fire_alert =
        commentary._fire (chime, IGNITING). Returns the stage for THIS pass:
        'idle' | 'loading' | 'igniting' | 'cooled'. NOTE: on the pass it fires
        IGNITING it returns 'igniting' even though it is now internally 'cooled'
        — so the caller can react (radar route) exactly once."""
        flip_near = (flip_level > 0 and abs(spot - flip_level) <= config.GAMMA_FLIP_NEAR_PTS)
        # LOADING gate: reuse signals.market_state COILING + convexity loaded.
        # flip_near is a BONUS, not a requirement (the flip is often undefined).
        load_ok = (regime_state == "COILING" and gamma_heat >= config.GAMMA_LOAD_HEAT)

        if self.stage == "cooled":
            if now - self.cooled_since >= config.GAMMA_COOLED_SEC:
                self.stage = "idle"
                self.load_start = 0.0
            return self.stage

        if self.stage == "idle":
            if load_ok:
                if self.load_start == 0.0:
                    self.load_start = now
                elif now - self.load_start >= GAMMA_LOAD_DWELL:
                    self.stage = "loading"
                    self.load_since = now
                    self.coil_mid = flip_level if flip_near else spot
                    self._decay_start = 0.0
                    self._fire_loading(spot, flip_level, now, fire_note)
            else:
                self.load_start = 0.0
            return self.stage

        if self.stage == "loading":
            if self._ignite_armed(spot, prev_gex, gex, realized_now, realized_then, now):
                self.dir = "up" if spot >= self.coil_mid else "down"
                self._fire_igniting(spot, flip_level, now, fire_alert)
                self.stage = "cooled"
                self.cooled_since = now
                return "igniting"      # signal the caller THIS pass (now internally cooled)
            # de-escalate SILENTLY if the coil dies (range opens / heat drops)
            if not load_ok:
                if self._decay_start == 0.0:
                    self._decay_start = now
                elif now - self._decay_start >= GAMMA_LOAD_DECAY:
                    self.stage = "idle"
                    self.load_start = 0.0
                    self._decay_start = 0.0
            else:
                self._decay_start = 0.0
            return self.stage
        return self.stage

    def _ignite_armed(self, spot, prev_gex, gex, realized_now, realized_then, now) -> bool:
        if now - self.load_since > config.GAMMA_IGNITE_FROM_LOAD_SEC:
            return False
        # MANDATORY: price has decisively left the coil band
        if abs(spot - self.coil_mid) < config.GAMMA_BREAK_PTS:
            return False
        # ONE genuinely INDEPENDENT confirmation (not the break re-counted):
        rv_kick = (realized_then > 0 and
                   (realized_now - realized_then) / realized_then >= config.REALIZED_KICK_FRAC)
        gex_flipped = (prev_gex >= 0.0 and gex < 0.0)   # a NEW negative regime, not standing-negative
        return rv_kick or gex_flipped or self.flow_accel

    def _fire_loading(self, spot, flip, now, note):
        if now - self._last_loading_fire < config.BLASTER_LOADING_COOLDOWN:
            return
        self._last_loading_fire = now
        ref = f"{flip:.0f}" if flip > 0 else f"{spot:.0f}"
        note(_fill(_pool(_BLASTER_LOADING, now), flip=ref), kind="blaster_loading")

    def _fire_igniting(self, spot, flip, now, fire):
        ref = f"{flip:.0f}" if flip > 0 else f"{spot:.0f}"
        txt = _fill(_pool(_BLASTER_IGNITING, now), flip=ref, dir=self.dir)
        self.last_ignite_text = txt
        fire("blaster_igniting", txt)      # own 600s cooldown bucket (see commentary._fire)
