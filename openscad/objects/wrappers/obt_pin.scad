// orcad wrapper for jaydee69/open-bricks-technic (MIT): obtPinBase().
use <open-bricks-technic-0465e456/parts/pins/ObtPinBase.scad>

pin_double_slit = false;
pin_rotate_slit = false;
pin_mech_stop = true;
fit_scale = 1;

scale(fit_scale) obtPinBase(doubleSlit = pin_double_slit,
                            rotateSlit = pin_rotate_slit,
                            mechStop = pin_mech_stop);
