# The simulator profile

> **Status:** stub — content to be written.

**Audience:** anyone learning capa without hardware.
**Scope:** every simulated device (Watlow, Alicat, Sartorius, NI-DAQ block + polled, webcam, FLIR IR), what they emit, how to tune them.

## Will cover

- Why simulators exist (CI, training, plugin development)
- The shipped sim configs: ``sim_freerun.yaml``, ``sim_capa_pyrolysis.yaml``
- How each sim adapter generates samples (signals.py)
- Tweaking sim parameters for realistic load
- Running the full CAPA procedure against simulators
- Limitations — what sims do NOT model (real saturation, IR radiometric noise floor, etc.)

*See also:* [Devices: Simulators](../devices/simulators.md).

