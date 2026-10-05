```yaml title="configs/experiments/sim_capa_pyrolysis.yaml"
# References to external files resolve relative to this YAML's directory.
hardware: ../hardware/sim_capa.toml
method:   ../methods/sim_capa_pyrolysis.method.toml

procedure:
  id: capa.builtin.recipe_runner   # plugin id; matched against plugins.lock
  version: "0.1"
  config:
    auto_acknowledge_prompts: true
    notes: "CAPA sim smoke: ramp under N2, soak, cool down."

domain_profile:
  id: capa.profiles.capa_pyrolysis  # CAPA scientific layer
  metadata:
    specimen:
      id: SIM-CAPA-001
      material: PMMA
      initial_mass_g: 5.0
      form: disk
      specimen_holder: "stainless steel cup"
      conditioning: "23C / 50% RH for 48h"
    program:
      target_heat_flux_kw_m2: 50.0
      heater_setpoint_c: 600.0
    atmosphere:
      mode: inert
      purge:
        species: N2
        purity: "UHP 5.0"
        target_flow_sccm: 100.0

device_settings:                   # applied to the devices when the config loads
  purge_mfc:
    gas: N2
  balance:
    filter_mode: very stable
    stability_range: accurate
  ir_cam0:
    temperature_range: {min_c: 0.0, max_c: 650.0}
    emissivity: 0.95
    distance_m: 0.5

calibration_set:
  name: default

operator:
  id: abr
  display_name: A. Researcher

sample:
  id: SIM-CAPA-001
  material: PMMA
  mass_g: 5.0

tags: [sim, capa, pyrolysis]
```
